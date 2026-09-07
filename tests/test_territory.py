"""Building the search list, and not quietly spending the user's money on it."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text, stop_reason="end_turn"):
        self.content, self.stop_reason = [_Block(text)], stop_reason


class _Messages:
    def __init__(self, text):
        self.text, self.calls = text, []

    def create(self, **kw):
        self.calls.append(kw)
        return _Msg(self.text)


class _Fake:
    def __init__(self, text):
        self.messages = _Messages(text)


class TerritoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-terr-")
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
        cfg = self.core.load_config()
        cfg.update(anthropic_api_key="sk-ant-x", saved_searches="",
                   searches_per_run=10, search_interval_hours=12)
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        # the cursor is meant to persist between runs, so each test starts it
        self.dash.STATE.db.set_kv("search_cursor", "0")

    def local(self, method, path, **kw):
        kw.setdefault("environ_base", {"REMOTE_ADDR": "127.0.0.1"})
        return getattr(self.client, method)(path, **kw)

    def stub(self, text):
        self.dash.STATE.services._anthropic = _Fake(text)

    # -- turning one town into a territory ------------------------------------

    def test_it_reads_a_clean_list_of_towns(self):
        self.stub("Napanoch, NY\nEllenville, NY\nKerhonkson, NY")
        towns = self.dash.STATE.services.towns_near("Napanoch, NY", 30)
        self.assertEqual(towns, ["Napanoch, NY", "Ellenville, NY",
                                 "Kerhonkson, NY"])

    def test_it_strips_the_chatter_around_the_list(self):
        self.stub("Here are the towns:\n\n1. Napanoch, NY\n- Ellenville, NY\n"
                  "* Kerhonkson, NY\n\nHope that helps!")
        self.assertEqual(self.dash.STATE.services.towns_near("Napanoch, NY", 30),
                         ["Napanoch, NY", "Ellenville, NY", "Kerhonkson, NY"])

    def test_duplicates_are_dropped(self):
        self.stub("Ellenville, NY\nellenville, ny\nKingston, NY")
        self.assertEqual(len(self.dash.STATE.services.towns_near("x", 30)), 2)

    def test_it_refuses_rather_than_inventing_an_empty_territory(self):
        self.stub("I couldn't find anything for that.")
        with self.assertRaises(self.core.ServiceError):
            self.dash.STATE.services.towns_near("Nowhereville", 30)

    def test_building_writes_every_trade_in_every_town(self):
        self.stub("Napanoch, NY\nEllenville, NY")
        self.local("post", "/action/build_searches",
                   data={"territory_base": "Napanoch, NY",
                         "territory_miles": "30",
                         "trades": "plumbers\nroofers"})
        lines = self.core.load_config()["saved_searches"].splitlines()
        self.assertCountEqual(lines, [
            "plumbers in Napanoch, NY", "roofers in Napanoch, NY",
            "plumbers in Ellenville, NY", "roofers in Ellenville, NY"])

    def test_building_needs_a_town_and_a_trade(self):
        self.stub("Napanoch, NY")
        self.local("post", "/action/build_searches",
                   data={"territory_base": "", "trades": "plumbers"})
        self.assertEqual(self.core.load_config()["saved_searches"], "")

    # -- the money ------------------------------------------------------------

    def test_a_run_spends_only_its_budget(self):
        cfg = self.core.load_config()
        cfg.update(saved_searches="\n".join("q%d" % i for i in range(50)),
                   searches_per_run=5, auto_search_enabled=True)
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        ran = []
        self.dash.STATE.agent.find_leads = lambda q: (ran.append(q),
                                                      {"added": 0})[1]
        self.dash.STATE.agent.run_saved_searches(force=True)
        self.assertEqual(len(ran), 5, "a run must not sweep the whole list")

    def test_the_next_run_carries_on_where_it_stopped(self):
        cfg = self.core.load_config()
        cfg.update(saved_searches="\n".join("q%d" % i for i in range(10)),
                   searches_per_run=4, auto_search_enabled=True)
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        ran = []
        self.dash.STATE.agent.find_leads = lambda q: (ran.append(q),
                                                      {"added": 0})[1]
        self.dash.STATE.agent.run_saved_searches(force=True)
        first = list(ran)
        ran.clear()
        self.dash.STATE.agent.run_saved_searches(force=True)
        self.assertEqual(first, ["q0", "q1", "q2", "q3"])
        self.assertEqual(ran, ["q4", "q5", "q6", "q7"])

    def test_it_wraps_round_the_list(self):
        cfg = self.core.load_config()
        cfg.update(saved_searches="a\nb\nc", searches_per_run=2,
                   auto_search_enabled=True)
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        self.dash.STATE.db.set_kv("search_cursor", "2")
        ran = []
        self.dash.STATE.agent.find_leads = lambda q: (ran.append(q),
                                                      {"added": 0})[1]
        self.dash.STATE.agent.run_saved_searches(force=True)
        self.assertEqual(ran, ["c", "a"])

    def test_the_cost_note_warns_when_it_would_charge_you(self):
        cheap = self.dash._search_cost(
            {"saved_searches": "\n".join("q%d" % i for i in range(40)),
             "searches_per_run": 10, "search_interval_hours": 12})
        self.assertFalse(cheap["over"], "20 searches a day should be free")

        pricey = self.dash._search_cost(
            {"saved_searches": "\n".join("q%d" % i for i in range(400)),
             "searches_per_run": 60, "search_interval_hours": 1})
        self.assertTrue(pricey["over"])
        self.assertGreater(int(pricey["dollars"]), 0)

    def test_the_cost_note_survives_an_empty_list(self):
        c = self.dash._search_cost({"saved_searches": "", "searches_per_run": 10,
                                    "search_interval_hours": 12})
        self.assertEqual(c["calls_month"], 0)
        self.assertFalse(c["over"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
