"""The built-in Ask helper: what it sees, what it stores, and what it can't do."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Message:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    """Captures the request instead of calling Anthropic."""

    def __init__(self, reply="Two leads are waiting on you.", stop_reason="end_turn"):
        self.reply, self.stop_reason, self.calls = reply, stop_reason, []

    def stream(self, **kw):
        self.calls.append(kw)
        return _FakeStream(_Message(self.reply, self.stop_reason))


class _FakeAnthropic:
    def __init__(self, messages):
        self.messages = messages


class AssistantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-ask-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        import solo_studio_agent as core
        import dashboard_app as dash
        cls.core, cls.dash = core, dash
        dash.STATE.reload()
        cls.client = dash.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        db = self.dash.STATE.db
        db.chat_clear()
        with db._conn() as c:
            c.execute("DELETE FROM leads")
            c.execute("DELETE FROM events")
        cfg = self.core.load_config()
        cfg["anthropic_api_key"] = "sk-ant-test"
        self.core.save_config(cfg)
        self.dash.STATE.reload()

    def local(self, method, path, **kw):
        kw.setdefault("environ_base", {"REMOTE_ADDR": "127.0.0.1"})
        return getattr(self.client, method)(path, **kw)

    def _stub(self, **kw):
        fake = _FakeMessages(**kw)
        self.dash.STATE.services._anthropic = _FakeAnthropic(fake)
        return fake

    # -- what the helper is allowed to be ----------------------------------

    def test_brief_forbids_acting(self):
        """The helper must never present itself as able to send or spend."""
        brief = self.core.Services.ASSISTANT_BRIEF
        self.assertIn("CANNOT act", brief)
        for claim in ("send email", "create a payment link", "deploy a site",
                      "move money"):
            self.assertIn(claim, brief)
        self.assertIn("PAYMENT GATE", brief)

    def test_no_tools_are_wired_up(self):
        """Structural guarantee: with no tools it cannot act, whatever it says."""
        fake = self._stub()
        self.dash.STATE.services.assistant_reply(
            [{"role": "user", "content": "hi"}], "SNAPSHOT")
        self.assertNotIn("tools", fake.calls[0])

    # -- the request it builds ---------------------------------------------

    def test_snapshot_is_sent_as_system_context(self):
        fake = self._stub()
        self.dash.STATE.services.assistant_reply(
            [{"role": "user", "content": "hi"}], "SNAPSHOT-MARKER")
        call = fake.calls[0]
        # The Ask box stays on the good model even under the frugal dial: you
        # use it a handful of times a month and by hand.
        self.assertEqual(call["model"], "claude-opus-5")
        joined = " ".join(part["text"] for part in call["system"])
        self.assertIn("SNAPSHOT-MARKER", joined)
        self.assertIn("Solo Studio", joined)
        self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])

    def test_refusal_gives_a_plain_answer_not_a_crash(self):
        self._stub(reply="", stop_reason="refusal")
        out = self.dash.STATE.services.assistant_reply(
            [{"role": "user", "content": "hi"}], "S")
        self.assertIn("wasn't able to answer", out)

    # -- the snapshot -------------------------------------------------------

    def test_snapshot_reports_missing_keys_and_empty_pipeline(self):
        snap = self.dash.assistant_snapshot()
        self.assertIn("still missing", snap)
        self.assertIn("Google Places", snap)
        self.assertIn("No leads yet", snap)

    def test_snapshot_reports_real_leads(self):
        db = self.dash.STATE.db
        lid = db.add_lead(place_id="p1", name="Rivera Plumbing", address="1 Main St",
                          phone="", category="plumber", email="hi@rivera.example")
        db.add_lead(place_id="p2", name="Anchor Barbershop", address="2 Main St",
                    phone="", category="barber", email="hi@anchor.example")
        db.claim(lid, [self.core.STAGE_FOUND], self.core.STAGE_PREVIEW_SENT)
        db.log(lid, "preview_emailed", "Preview sent to Rivera Plumbing")
        db.log(None, "needs_attention", "Anchor asked about timing", True)
        snap = self.dash.assistant_snapshot()
        self.assertIn("Rivera Plumbing", snap)
        self.assertIn("preview_sent", snap)
        self.assertIn("WAITING FOR THEIR APPROVAL", snap)
        self.assertIn("Anchor Barbershop", snap)
        self.assertIn("NEEDS THEIR ATTENTION", snap)
        self.assertIn("Anchor asked about timing", snap)

    def test_snapshot_stays_bounded_with_many_leads(self):
        db = self.dash.STATE.db
        for i in range(200):
            db.add_lead(place_id=f"bulk-{i}", name=f"Business {i}", address="x",
                        phone="", category="shop", email=f"b{i}@example.com")
        snap = self.dash.assistant_snapshot()
        self.assertLess(len(snap), 8000, "snapshot must not grow without bound")
        self.assertIn("and 192 more", snap)

    # -- the route ----------------------------------------------------------

    def test_ask_page_renders(self):
        self.assertEqual(self.local("get", "/ask").status_code, 200)

    def test_send_stores_both_sides_of_the_turn(self):
        self._stub(reply="You have two leads waiting.")
        r = self.local("post", "/ask/send", json={"message": "what's waiting?"})
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["reply"], "You have two leads waiting.")
        history = [(m["role"], m["content"]) for m in self.dash.STATE.db.chat_history()]
        self.assertEqual(history, [("user", "what's waiting?"),
                                   ("assistant", "You have two leads waiting.")])

    def test_history_is_replayed_so_it_remembers(self):
        fake = self._stub()
        self.local("post", "/ask/send", json={"message": "first"})
        self.local("post", "/ask/send", json={"message": "second"})
        sent = [m["content"] for m in fake.calls[-1]["messages"]]
        self.assertIn("first", sent)
        self.assertIn("second", sent)

    def test_empty_message_is_refused(self):
        r = self.local("post", "/ask/send", json={"message": "   "})
        self.assertFalse(r.json["ok"])

    def test_missing_key_explains_instead_of_failing(self):
        cfg = self.core.load_config()
        cfg["anthropic_api_key"] = ""
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        r = self.local("post", "/ask/send", json={"message": "hello"})
        self.assertFalse(r.json["ok"])
        self.assertIn("Setup", r.json["error"])

    def test_service_failure_is_reported_not_a_500(self):
        class Boom:
            def stream(self, **kw):
                raise RuntimeError("connection reset")
        self.dash.STATE.services._anthropic = _FakeAnthropic(Boom())
        r = self.local("post", "/ask/send", json={"message": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json["ok"])
        self.assertIn("connection reset", r.json["error"])

    # -- the JARVIS console -------------------------------------------------

    def test_jarvis_carries_the_ask_console(self):
        html = self.local("get", "/jarvis").data.decode()
        for piece in ('id="askbar"', 'id="askq"', 'id="console"', 'id="lines"',
                      "'/ask/send'", "'/ask/history'"):
            self.assertIn(piece, html)

    def test_jarvis_console_is_never_hidden_on_phones(self):
        """The ask bar is how you reach it — a media query must not remove it."""
        import re
        html = self.local("get", "/jarvis").data.decode()
        for block in re.findall(r"@media[^{]*max-width[^{]*\{(.*?)\n\}", html, re.S):
            # the bar itself, not its children (#askbar .hint is allowed to hide)
            for rule in re.findall(r"#askbar\s*\{([^{}]*)\}", block):
                self.assertNotIn("display:none", rule.replace(" ", ""))

    def test_history_endpoint_returns_the_saved_thread(self):
        self._stub(reply="noted")
        self.local("post", "/ask/send", json={"message": "remember this"})
        r = self.local("get", "/ask/history")
        self.assertTrue(r.json["ok"])
        self.assertTrue(r.json["has_key"])
        self.assertEqual([m["content"] for m in r.json["messages"]],
                         ["remember this", "noted"])

    def test_history_flags_a_missing_key(self):
        cfg = self.core.load_config()
        cfg["anthropic_api_key"] = ""
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        self.assertFalse(self.local("get", "/ask/history").json["has_key"])

    def test_one_thread_across_both_screens(self):
        """Asking on JARVIS and asking on /ask share the same conversation."""
        self._stub(reply="same thread")
        self.local("post", "/ask/send", json={"message": "asked from jarvis"})
        seen = [m["content"] for m in self.local("get", "/ask/history").json["messages"]]
        self.assertIn("asked from jarvis", seen)
        page = self.local("get", "/ask").data.decode()
        self.assertIn("asked from jarvis", page)

    def test_clear_empties_the_chat(self):
        self._stub()
        self.local("post", "/ask/send", json={"message": "hello"})
        self.assertTrue(self.dash.STATE.db.chat_history())
        self.local("post", "/ask/clear")
        self.assertEqual(self.dash.STATE.db.chat_history(), [])

    def test_chat_survives_a_restart(self):
        self._stub(reply="remembered")
        self.local("post", "/ask/send", json={"message": "before restart"})
        fresh = self.core.Database()           # reopens the same file
        self.assertIn("before restart",
                      [m["content"] for m in fresh.chat_history()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
