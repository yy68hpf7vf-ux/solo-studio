"""JARVIS keeping watch.

He reads everything and tells the owner what needs them. He never acts: no
email, no money, no stage changes. These pin down both halves.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class WatchmanTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-watch-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.db = core.Database()
        self.cfg = dict(core.DEFAULT_CONFIG)
        for _, field in core.API_KEYS:
            self.cfg[field] = "x"
        self.cfg.update(your_name="Sam", mailing_address="1 Main St",
                        autopilot_enabled=True, auto_search_enabled=True,
                        saved_searches="roofers in Atlanta, GA")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def look(self, **over):
        cfg = dict(self.cfg, **over)
        return {f["id"]: f for f in self.core.checkup(self.db, cfg)}

    def add(self, name="Joe", stage=None, **kw):
        lead_id = self.db.add_lead(place_id=name, name=name, address="a",
                                   phone="p", category="c", **kw)
        if stage:
            self.db.update_lead(lead_id, stage=stage)
        return lead_id

    # -- a healthy app is quiet ---------------------------------------------

    def test_a_set_up_app_with_nothing_wrong_says_nothing(self):
        self.assertEqual(self.look(), {})

    # -- it can't work -------------------------------------------------------

    def test_missing_keys(self):
        f = self.look(anthropic_api_key="", stripe_secret_key="")["keys-missing"]
        self.assertEqual(f["level"], self.core.FIX)
        self.assertIn("Anthropic", f["detail"])
        self.assertIn("Stripe", f["detail"])

    def test_a_missing_mailing_address_is_a_fault_because_the_law_says_so(self):
        self.assertEqual(self.look(mailing_address="")["no-mailing-address"]["level"],
                         self.core.FIX)

    def test_phone_access_with_no_pin_is_a_fault(self):
        f = self.look(phone_access_enabled=True, phone_pin="")["phone-open"]
        self.assertEqual(f["level"], self.core.FIX)

    def test_phone_access_with_a_pin_is_fine(self):
        self.assertNotIn("phone-open",
                         self.look(phone_access_enabled=True, phone_pin="1234"))

    def test_it_notices_the_account_ran_out_of_credit(self):
        self.db.log(None, "auto_search_failed",
                    "Search failed: This Anthropic account has $0 of API credit.")
        self.assertIn("credit-empty", self.look())

    # -- leads that are stuck or broken --------------------------------------

    def test_a_lead_that_gave_up_is_reported(self):
        self.add("Broken Co", stage=self.core.STAGE_ERROR)
        f = self.look()["leads-error"]
        self.assertEqual(f["level"], self.core.FIX)
        self.assertIn("Broken Co", f["detail"])

    def test_a_lead_wedged_mid_step_is_reported(self):
        lead_id = self.add("Wedged", stage=self.core.STAGE_BUILDING_PREVIEW)
        old = "2020-01-01T00:00:00+00:00"
        with self.db._conn() as c:
            c.execute("UPDATE leads SET updated_at=? WHERE id=?", (old, lead_id))
        self.assertEqual(self.look()["leads-stuck"]["level"], self.core.FIX)

    def test_a_lead_that_keeps_failing_is_stuck_even_though_it_looks_fresh(self):
        """Each retry refreshes the lead, so age alone never catches this —
        which is exactly the case worth catching."""
        lead_id = self.add("Retrying", stage=self.core.STAGE_BUILDING_PREVIEW)
        self.db.update_lead(lead_id, attempts=self.core.STUCK_ATTEMPTS)
        f = self.look()["leads-stuck"]
        self.assertEqual(f["level"], self.core.FIX)
        self.assertIn("retrying", f["detail"])

    def test_one_failed_attempt_is_not_yet_a_problem(self):
        lead_id = self.add("Blipped", stage=self.core.STAGE_BUILDING_PREVIEW)
        self.db.update_lead(lead_id, attempts=1)
        self.assertNotIn("leads-stuck", self.look())

    def test_a_lead_that_only_just_started_a_step_is_left_alone(self):
        self.add("Busy", stage=self.core.STAGE_BUILDING_PREVIEW)
        self.assertNotIn("leads-stuck", self.look())

    # -- waiting on the human ------------------------------------------------

    def test_the_approval_queue_is_waiting_not_broken(self):
        self.add("Ready", email="a@b.com")
        f = self.look()["approve-queue"]
        self.assertEqual(f["level"], self.core.WAITING)
        self.assertEqual(f["where"], "/approve")

    def test_a_lead_with_no_email_is_waiting(self):
        self.add("Nameless")
        self.assertEqual(self.look()["need-email"]["level"], self.core.WAITING)

    # -- ordering ------------------------------------------------------------

    def test_the_worst_thing_is_always_first(self):
        self.add("Ready", email="a@b.com")                       # waiting
        self.add("Broken", stage=self.core.STAGE_ERROR)          # fix
        found = self.core.checkup(self.db, dict(self.cfg, autopilot_enabled=False))
        levels = [f["level"] for f in found]
        self.assertEqual(levels, sorted(levels, key=self.core.LEVEL_ORDER.get))
        self.assertEqual(found[0]["level"], self.core.FIX)

    # -- switched off --------------------------------------------------------

    def test_jarvis_being_switched_off_is_the_loudest_thing_on_the_screen(self):
        """Nothing happening and nothing said is the failure this app kept
        having. If he isn't working, that is a fault, not a footnote."""
        f = self.look(autopilot_enabled=False)["autopilot-off"]
        self.assertEqual(f["level"], self.core.FIX)
        self.assertIn("switched off", f["title"])

    def test_hunting_being_off_is_a_fault_too(self):
        f = self.look(auto_search_enabled=False)["search-off"]
        self.assertEqual(f["level"], self.core.FIX)

    def test_a_used_up_search_budget_says_so_rather_than_going_quiet(self):
        f = self.core.checkup(self.db, self.cfg, spent=5000, cap=4500)
        ids = {x["id"]: x for x in f}
        self.assertIn("google-spent", ids)
        self.assertEqual(ids["google-spent"]["level"], self.core.FIX)
        self.assertIn("5000", ids["google-spent"]["detail"])

    def test_a_budget_with_room_left_says_nothing(self):
        ids = {x["id"] for x in self.core.checkup(self.db, self.cfg,
                                                  spent=10, cap=4500)}
        self.assertNotIn("google-spent", ids)

    def test_automation_is_on_out_of_the_box(self):
        self.assertTrue(self.core.DEFAULT_CONFIG["autopilot_enabled"])
        self.assertTrue(self.core.DEFAULT_CONFIG["auto_search_enabled"])

    def test_a_test_mode_stripe_key_is_flagged_as_practice_not_failure(self):
        f = self.look(stripe_secret_key="sk_test_123")["stripe-test"]
        self.assertEqual(f["level"], self.core.WATCH)

    # -- every finding has to be usable --------------------------------------

    def test_every_finding_says_where_to_go(self):
        self.add("Ready", email="a@b.com")
        self.add("Broken", stage=self.core.STAGE_ERROR)
        for f in self.core.checkup(self.db, dict(self.cfg, anthropic_api_key="",
                                                 autopilot_enabled=False)):
            with self.subTest(finding=f["id"]):
                self.assertTrue(f["title"] and f["detail"])
                self.assertTrue(f["where"].startswith("/"))
                self.assertIn(f["level"], (self.core.FIX, self.core.WAITING,
                                           self.core.WATCH))
                self.assertLess(len(f["title"]), 60)


class NotifyingTest(unittest.TestCase):
    """He should buzz once when something breaks — not every two minutes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-watchn-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        cfg = dict(core.DEFAULT_CONFIG)
        for _, field in core.API_KEYS:
            cfg[field] = "x"
        cfg.update(your_name="Sam", mailing_address="1 Main St",
                   autopilot_enabled=True, auto_search_enabled=True,
                   saved_searches="roofers in Atlanta, GA")
        core.save_config(cfg)
        self.agent = core.Agent(core.Database(), core.Services(cfg), cfg)
        self.pushes = []
        self.agent.services.push_notify = (
            lambda t, m, priority="default", tags="": self.pushes.append((t, m)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    _broken = 0

    def break_something(self):
        """A distinct place each time — a repeat place_id is just a duplicate
        and would silently add nothing."""
        NotifyingTest._broken += 1
        lead_id = self.agent.db.add_lead(
            place_id=f"p{self._broken}", name=f"Broken {self._broken}",
            address="a", phone="p", category="c", email="a@b.com")
        self.assertIsNotNone(lead_id)
        self.agent.db.update_lead(lead_id, stage=self.core.STAGE_ERROR)

    def test_nothing_wrong_means_no_notification(self):
        self.agent.watch()
        self.assertEqual(self.pushes, [])

    def test_a_new_fault_notifies_once_and_then_shuts_up(self):
        self.break_something()
        self.agent.watch()
        self.assertEqual(len(self.pushes), 1)
        self.agent.watch()
        self.agent.watch()
        self.assertEqual(len(self.pushes), 1, "it must not buzz every tick")

    def test_a_fault_that_clears_and_comes_back_notifies_again(self):
        self.break_something()
        self.agent.watch()
        for lead in self.agent.db.all_leads():
            self.agent.db.update_lead(lead["id"], stage=self.core.STAGE_DELIVERED)
        self.agent.watch()
        self.break_something()
        self.agent.watch()
        self.assertEqual(len(self.pushes), 2)

    def test_waiting_on_you_does_not_buzz_the_phone(self):
        """A queue to review is not an emergency; only faults push."""
        self.agent.db.add_lead(place_id="q", name="Ready", address="a",
                               phone="p", category="c", email="a@b.com")
        self.agent.watch()
        self.assertEqual(self.pushes, [])

    def test_watching_never_touches_a_lead(self):
        """The whole point of it being safe: it reads, it does not act."""
        self.break_something()
        before = [dict(l) for l in self.agent.db.all_leads()]
        self.agent.watch()
        after = [dict(l) for l in self.agent.db.all_leads()]
        self.assertEqual(before, after)

    def test_watching_never_sends_anything(self):
        import inspect
        src = inspect.getsource(self.core.Agent.watch)
        for act in ("send_outreach", "email_send", "checkout", "deploy",
                    "update_lead", "claim"):
            self.assertNotIn(act, src)


class ItReachesTheScreenTest(unittest.TestCase):
    """The watch list was briefly wired to the wrong endpoint — it went out on
    the refresh poll, which ignores it, and never reached the JARVIS screen,
    which needs it. Only a browser caught that. These pin the contract."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-screen-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        import dashboard_app as dash
        import solo_studio_agent as core
        cls.dash, cls.core = dash, core
        dash.STATE.reload()
        cls.client = dash.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def get(self, path):
        return self.client.get(path, environ_base={"REMOTE_ADDR": "127.0.0.1"})

    def test_the_jarvis_screen_is_sent_the_watch_list(self):
        data = self.get("/jarvis/data").json
        self.assertIn("findings", data)
        self.assertTrue(data["findings"], "a half-set-up app has plenty to say")
        for f in data["findings"]:
            self.assertEqual(set(f), {"id", "level", "title", "detail",
                                      "where", "cta"})

    def test_the_screen_reads_the_field_that_is_actually_sent(self):
        """A rename on either side breaks it silently, and the page falls back
        to LINK LOST with no clue why."""
        page = self.get("/jarvis").data.decode()
        self.assertIn("d.findings", page)
        self.assertIn('id="alerts"', page)

    def test_the_dashboard_shows_it_too(self):
        html = self.get("/").data.decode()
        self.assertIn("watchcard", html)
        self.assertIn("JARVIS", html)

    def test_the_refresh_poll_is_not_burdened_with_it(self):
        """It goes out every few seconds and nothing there reads it."""
        self.assertNotIn("findings", self.get("/live").json)

    def test_the_assistant_is_told_what_is_wrong(self):
        brief = self.dash.assistant_snapshot()
        self.assertIn("WHAT NEEDS THEM RIGHT NOW", brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
