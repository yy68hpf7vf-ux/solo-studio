"""The studio: every lead shown in the room it is actually standing in."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class HouseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-house-")
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
        for k in ("google_places_api_key", "inkbox_api_key", "anthropic_api_key",
                  "netlify_api_key", "stripe_secret_key"):
            cfg[k] = ""
        cfg["autopilot_enabled"] = False
        cfg["auto_search_enabled"] = False
        cfg["auto_research_enabled"] = False
        self.core.save_config(cfg)
        self.dash.STATE.reload()

    def local(self, path):
        return self.client.get(path, environ_base={"REMOTE_ADDR": "127.0.0.1"})

    def add(self, name, stage, email="a@b.example"):
        db = self.dash.STATE.db
        lid = db.add_lead(place_id=name, name=name, address="1 St", phone="",
                          category="shop", email=email)
        if stage != self.core.STAGE_FOUND:
            db.claim(lid, [self.core.STAGE_FOUND], stage)
        return lid

    def rooms(self):
        return {r["key"]: r for r in self.local("/house/data").json["rooms"]}

    # -- the layout ---------------------------------------------------------

    def test_every_room_has_somewhere_to_be_and_a_colour(self):
        keys = [k for k, _s, _i, _n, _d in self.dash.HOUSE_ROOMS]
        self.assertEqual(len(keys), len(set(keys)), "duplicate room")
        for k, storey, icon, name, doing in self.dash.HOUSE_ROOMS:
            with self.subTest(room=k):
                self.assertIn(storey, (0, 1, 3))
                self.assertTrue(icon and name and doing)
                self.assertIn(k, self.dash.ROOM_TINT)

    def test_page_draws_all_eight_rooms(self):
        html = self.local("/house").data.decode()
        self.assertEqual(self.local("/house").status_code, 200)
        for _k, _s, _i, name, _d in self.dash.HOUSE_ROOMS:
            self.assertIn(name, html)

    # -- who is holding what ------------------------------------------------

    def test_a_lead_shows_up_in_exactly_one_room(self):
        self.add("Rivera Plumbing", self.core.STAGE_PREVIEW_SENT)
        rooms = self.rooms()
        self.assertEqual(rooms["deployer"]["count"], 1)
        for other in ("triage", "designer", "biller", "delivery"):
            self.assertEqual(rooms[other]["count"], 0, other)

    def test_leads_move_between_rooms_as_the_deal_moves(self):
        lid = self.add("Casa Bonita", self.core.STAGE_PREVIEW_SENT)
        self.assertEqual(self.rooms()["deployer"]["count"], 1)
        self.dash.STATE.db.claim(lid, [self.core.STAGE_PREVIEW_SENT],
                                 self.core.STAGE_PAYMENT_LINK_SENT)
        after = self.rooms()
        self.assertEqual(after["deployer"]["count"], 0)
        self.assertEqual(after["biller"]["count"], 1)

    def test_the_landing_counts_what_is_waiting_on_you(self):
        self.add("Anchor Barbershop", self.core.STAGE_FOUND)
        self.add("Vista Roofing", self.core.STAGE_FOUND)
        self.assertEqual(self.local("/house/data").json["you"]["waiting"], 2)

    def test_a_room_without_its_key_says_so(self):
        rooms = self.rooms()
        self.assertEqual(rooms["biller"]["state"], "nokey")
        self.assertIn("Stripe", rooms["biller"]["note"])

    def test_a_room_with_its_key_but_autopilot_off_is_standing_by(self):
        cfg = self.core.load_config()
        cfg["anthropic_api_key"] = "sk-ant-x"
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        self.assertEqual(self.rooms()["triage"]["state"], "standby")
        cfg["autopilot_enabled"] = True
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        self.assertEqual(self.rooms()["triage"]["state"], "on")

    def test_vault_reports_the_money(self):
        lid = self.add("Bloom Florals", self.core.STAGE_DELIVERED)
        with self.dash.STATE.db._conn() as c:
            c.execute("UPDATE leads SET paid_at=?, amount_cents=? WHERE id=?",
                      (self.core._now(), 50000, lid))
        v = self.local("/house/data").json["vault"]
        self.assertEqual(v["collected"], 500)
        self.assertEqual(v["delivered"], 1)

    def test_house_is_behind_the_gate(self):
        cfg = self.core.load_config()
        cfg.update(phone_access_enabled=True, phone_pin="2468")
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        with self.client.session_transaction() as sess:
            sess.clear()
        remote = {"REMOTE_ADDR": "10.0.0.9"}
        self.assertEqual(self.client.get("/house", environ_base=remote).status_code, 302)
        self.assertEqual(self.client.get("/house/data", environ_base=remote).status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
