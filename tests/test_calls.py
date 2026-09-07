"""The call list: the app lines them up, the human dials.

Nothing here may ever place a call or send a text on its own — automated cold
SMS and AI cold calls are a legal trap in the US, so the whole point of this
page is that it stops at a tel: link.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CallListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-calls-")
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
        with db._conn() as c:
            c.execute("DELETE FROM leads")
            c.execute("DELETE FROM events")
        cfg = self.core.load_config()
        cfg.update(your_name="Sam Rivera", studio_name="Solo Studio",
                   site_price_usd=500)
        self.core.save_config(cfg)
        self.dash.STATE.reload()

    def add(self, name, phone="", email=None):
        return self.dash.STATE.db.add_lead(
            place_id=name, name=name, address="1 Main St", phone=phone,
            category="plumber", email=email)

    def local(self, method, path, **kw):
        kw.setdefault("environ_base", {"REMOTE_ADDR": "127.0.0.1"})
        return getattr(self.client, method)(path, **kw)

    def listed(self):
        return [l["name"] for l in self.dash.STATE.db.leads_to_call()]

    # -- the boundary that matters --------------------------------------------

    def test_nothing_here_can_dial_or_text_by_itself(self):
        """The page must hand off to the phone, never send anything."""
        self.add("Rivera Plumbing", phone="(951) 555-0142")
        html = self.local("get", "/calls").data.decode()
        self.assertIn("tel:", html)
        self.assertIn("sms:", html)
        # no route on the whole app sends an SMS or places a call
        import dashboard_app as dash
        with open(dash.__file__, encoding="utf-8") as f:
            source = f.read()
        for banned in ("send_sms", "twilio", "place_call", "send_text"):
            self.assertNotIn(banned, source.lower())

    def test_the_page_says_why_it_does_not_automate(self):
        self.add("Rivera Plumbing", phone="(951) 555-0142")
        html = self.local("get", "/calls").data.decode()
        self.assertIn("never dial or text for you", html)

    # -- who shows up ---------------------------------------------------------

    def test_only_leads_with_a_phone_number(self):
        self.add("Has Phone", phone="(951) 555-0142")
        self.add("No Phone", phone="", email="a@b.example")
        self.assertEqual(self.listed(), ["Has Phone"])

    def test_untried_leads_come_first(self):
        self.add("Alpha", phone="111")
        self.add("Beta", phone="222")
        self.assertEqual(self.listed(), ["Alpha", "Beta"])
        self.local("post", "/action/call_logged/%d"
                   % self.dash.STATE.db.all_leads()[0]["id"])
        self.assertEqual(self.listed(), ["Beta", "Alpha"])

    def test_phone_only_leads_outrank_ones_you_could_email(self):
        self.add("Emailable", phone="111", email="a@b.example")
        self.add("Phone only", phone="222")
        self.assertEqual(self.listed()[0], "Phone only")

    def test_a_lead_that_said_no_never_comes_back(self):
        lid = self.add("Rivera Plumbing", phone="111")
        self.local("post", "/action/call_pass/%d" % lid)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.dash.STATE.db.get_lead(lid)["do_not_contact"], 1)

    # -- the useful outcome ---------------------------------------------------

    def test_an_email_from_a_call_joins_the_approval_queue(self):
        """A call is how a phone-only lead gets an email — and it still has to
        go past the human before anything sends."""
        lid = self.add("Rivera Plumbing", phone="111")
        self.local("post", "/action/call_email/%d" % lid,
                   data={"email": "owner@rivera.example"})
        lead = self.dash.STATE.db.get_lead(lid)
        self.assertEqual(lead["email"], "owner@rivera.example")
        self.assertIn("Rivera Plumbing",
                      [l["name"] for l in
                       self.dash.STATE.db.leads_awaiting_approval()])
        self.assertEqual(lead["stage"], self.core.STAGE_FOUND,
                         "a call must not skip the approval gate")

    def test_a_bad_email_is_rejected(self):
        lid = self.add("Rivera Plumbing", phone="111")
        self.local("post", "/action/call_email/%d" % lid,
                   data={"email": "not-an-email"})
        self.assertIsNone(self.dash.STATE.db.get_lead(lid)["email"])

    # -- what you say ---------------------------------------------------------

    def test_the_opener_uses_their_real_details(self):
        lead = {"name": "Rivera Plumbing"}
        opener = self.core.call_opener(lead, self.core.load_config())
        self.assertIn("Rivera Plumbing", opener)
        self.assertIn("Sam Rivera", opener)
        self.assertIn("$500", opener)
        self.assertIn("email", opener.lower())

    def test_the_opener_works_before_setup_is_filled_in(self):
        cfg = self.core.load_config()
        cfg.update(your_name="", studio_name="")
        opener = self.core.call_opener({"name": "Rivera Plumbing"}, cfg)
        self.assertTrue(opener.strip())
        self.assertNotIn("None", opener)

    def test_tel_links_are_stripped_of_formatting(self):
        self.add("Rivera Plumbing", phone="(951) 555-0142")
        html = self.local("get", "/calls").data.decode()
        self.assertIn("tel:9515550142", html)
        self.assertNotIn("tel:(951)", html)

    def test_calls_page_is_behind_the_gate(self):
        cfg = self.core.load_config()
        cfg.update(phone_access_enabled=True, phone_pin="2468")
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get(
            "/calls", environ_base={"REMOTE_ADDR": "10.0.0.9"}).status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
