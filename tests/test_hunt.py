"""JARVIS going and finding the leads himself.

From a real complaint: "I used Los Angeles and it didn't find anything, which
I don't believe — there are hundreds of thousands of businesses in the US."
Both halves were true. There are; and searching a business directory for a
city name returns the city and its biggest firms, all of which have websites.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class HuntTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-hunt-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k",
                   anthropic_api_key="k", territory_miles=30)
        core.save_config(cfg)
        self.agent = core.Agent(core.Database(), core.Services(cfg), cfg)
        self.ran = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def stub_places(self, per_query):
        """per_query(query) -> how many website-less businesses it turns up."""
        def fake(query, max_results=60):
            self.ran.append(query)
            n = per_query(query)
            out = self.core.SearchResults(
                {"place_id": f"{query}-{i}", "name": f"Biz {query} {i}",
                 "address": "a", "phone": "p", "category": "c",
                 "social_url": None} for i in range(n))
            out.seen = 20
            out.with_site = 20 - n
            return out
        self.agent.services.places_search_no_website = fake

    def stub_towns(self, towns):
        self.agent.services.towns_near = lambda base, miles: list(towns)

    # -- the Los Angeles case ------------------------------------------------

    def test_a_city_that_yields_nothing_still_finds_leads_in_its_suburbs(self):
        self.stub_towns(["Hawthorne, CA", "Bell Gardens, CA", "Los Angeles, CA"])
        self.stub_places(lambda q: 0 if "Los Angeles" in q else 3)
        r = self.agent.hunt("Los Angeles, CA")
        self.assertGreater(r["added"], 0)
        self.assertIn("Found", r["summary"])

    def test_it_stops_as_soon_as_it_has_enough(self):
        """A good area must not cost twelve searches."""
        self.stub_towns(["Hawthorne, CA"])
        self.stub_places(lambda q: 9)
        r = self.agent.hunt("Los Angeles, CA", want=8)
        self.assertEqual(r["searched"], 1)
        self.assertEqual(len(self.ran), 1)

    def test_it_never_runs_more_than_its_budget(self):
        self.stub_towns(["A, CA", "B, CA"])
        self.stub_places(lambda q: 0)
        r = self.agent.hunt("Los Angeles, CA", budget=5)
        self.assertEqual(r["searched"], 5)

    def test_it_sweeps_towns_rather_than_hammering_one(self):
        towns = ["Hawthorne, CA", "Bell Gardens, CA", "Compton, CA"]
        self.stub_towns(towns)
        self.stub_places(lambda q: 0)
        self.agent.hunt("Los Angeles, CA", budget=3)
        hit = {q.split(" in ", 1)[1] for q in self.ran}
        self.assertEqual(len(hit), 3, "three searches should visit three towns")

    def test_it_tries_different_trades(self):
        """It rotates the trade as it moves between towns, so a short hunt is
        never four goes at the same job."""
        self.stub_towns(["Hawthorne, CA"])
        self.stub_places(lambda q: 0)
        self.agent.hunt("Los Angeles, CA", budget=4)
        trades = {q.split(" in ", 1)[0] for q in self.ran}
        self.assertGreaterEqual(len(trades), 3)

    def test_it_never_runs_the_same_search_twice(self):
        self.stub_towns(["A, CA", "B, CA", "C, CA"])
        self.stub_places(lambda q: 0)
        self.agent.hunt("Los Angeles, CA", budget=12)
        self.assertEqual(len(self.ran), len(set(self.ran)))

    # -- it has to work when Claude is down ----------------------------------

    def test_no_town_list_still_hunts_the_place_itself(self):
        """Out of API credit is exactly when they most need this to work."""
        def boom(base, miles):
            raise self.core.ServiceError("This Anthropic account has $0 of API credit.")
        self.agent.services.towns_near = boom
        self.stub_places(lambda q: 2)
        r = self.agent.hunt("Los Angeles, CA", want=8, budget=4)
        self.assertGreater(r["added"], 0)
        self.assertTrue(all("Los Angeles, CA" in q for q in self.ran))
        self.assertIn("Couldn't work out the towns", r["summary"])

    # -- telling the truth ---------------------------------------------------

    def test_a_genuinely_dry_area_says_so_and_says_why(self):
        self.stub_towns(["Hawthorne, CA"])
        self.stub_places(lambda q: 0)
        r = self.agent.hunt("Los Angeles, CA", budget=3)
        self.assertEqual(r["added"], 0)
        self.assertIn("had websites already", r["summary"])
        self.assertIn("further out", r["summary"])

    def test_google_returning_nothing_reads_as_a_typo(self):
        self.stub_towns(["Nowhere, ZZ"])

        def fake(query, max_results=60):
            self.ran.append(query)
            return self.core.SearchResults()
        self.agent.services.places_search_no_website = fake
        r = self.agent.hunt("Lso Angeles", budget=2)
        self.assertIn("spelling", r["summary"])

    def test_a_search_that_blows_up_does_not_end_the_hunt(self):
        self.stub_towns(["A, CA", "B, CA"])
        calls = []

        def fake(query, max_results=60):
            calls.append(query)
            if len(calls) == 1:
                raise self.core.ServiceError("Google said no")
            out = self.core.SearchResults([{
                "place_id": query, "name": "Biz", "address": "a", "phone": "p",
                "category": "c", "social_url": None}])
            out.seen = 20
            return out
        self.agent.services.places_search_no_website = fake
        r = self.agent.hunt("Los Angeles, CA", budget=3)
        self.assertEqual(r["added"], 2)

    def test_the_hunt_is_written_down(self):
        self.stub_towns(["Hawthorne, CA"])
        self.stub_places(lambda q: 4)
        self.agent.hunt("Los Angeles, CA")
        kinds = [e["kind"] for e in self.agent.db.recent_events(20)]
        self.assertIn("hunt", kinds)


class KeepStockedTest(unittest.TestCase):
    """JARVIS restocking on his own — lazily, because it costs money."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-stock-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k",
                        auto_search_enabled=True,
                        territory_base="Los Angeles, CA", lead_floor=15)
        self.agent = core.Agent(core.Database(), core.Services(self.cfg), self.cfg)
        self.hunts = []
        self.agent.hunt = lambda area, **kw: (self.hunts.append(area),
                                              {"added": 3, "summary": "ok"})[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def stock(self, n):
        for i in range(n):
            self.agent.db.add_lead(place_id=f"p{i}", name=f"Biz {i}",
                                   address="a", phone="p", category="c")

    def test_an_empty_shelf_sends_him_hunting(self):
        self.agent.keep_stocked()
        self.assertEqual(self.hunts, ["Los Angeles, CA"])

    def test_a_full_shelf_does_not(self):
        self.stock(20)
        r = self.agent.keep_stocked()
        self.assertEqual(self.hunts, [])
        self.assertIn("still waiting", r["skipped"])

    def test_he_does_not_go_again_straight_away(self):
        self.agent.keep_stocked()
        self.agent.keep_stocked()
        self.assertEqual(len(self.hunts), 1, "hunting costs Google calls")

    def test_no_home_town_means_no_hunting(self):
        self.cfg["territory_base"] = ""
        self.assertIn("home town", self.agent.keep_stocked()["skipped"])

    def test_switching_automatic_hunting_off_stops_him(self):
        self.cfg["auto_search_enabled"] = False
        self.agent.keep_stocked()
        self.assertEqual(self.hunts, [])

    def test_it_runs_as_part_of_the_normal_round(self):
        import inspect
        self.assertIn("keep_stocked",
                      inspect.getsource(self.core.Agent.tick))


class GoogleMeterTest(unittest.TestCase):
    """Letting JARVIS work continuously is only safe if something says no on
    the owner's behalf."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-meter-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k",
                        monthly_google_cap=3)
        self.svc = core.Services(self.cfg)
        self.agent = core.Agent(core.Database(), self.svc, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def _one_page(self):
        return type("R", (), {"status_code": 200,
                              "json": lambda self: {"places": []}})()

    def test_every_google_call_is_counted(self):
        with mock.patch.object(self.core.requests, "post",
                               return_value=self._one_page()):
            self.svc.places_search_no_website("roofers in LA")
            self.svc.places_search_no_website("plumbers in LA")
        self.assertEqual(self.agent.google_calls_this_month(), 2)

    def test_it_refuses_once_the_cap_is_reached(self):
        with mock.patch.object(self.core.requests, "post",
                               return_value=self._one_page()):
            for _ in range(3):
                self.svc.places_search_no_website("q")
            with self.assertRaises(self.core.ServiceError) as caught:
                self.svc.places_search_no_website("one too many")
        self.assertIn("cap", str(caught.exception))

    def test_the_default_cap_sits_under_googles_free_allowance(self):
        self.assertLess(self.core.DEFAULT_CONFIG["monthly_google_cap"],
                        self.core.GOOGLE_FREE_CALLS_MONTH)

    def test_the_count_is_per_month(self):
        key = self.core._google_meter_key()
        self.assertIn(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m"), key)


class WhatCountsAsATradeTest(unittest.TestCase):
    """A bare place name is the trap. It has to be recognised as one."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-trade-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        import dashboard_app as dash
        cls.dash = dash

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def test_a_bare_city_is_not_a_trade(self):
        for place in ("Los Angeles", "los angeles, ca", "Atlanta, Georgia",
                      "Napanoch, NY"):
            with self.subTest(place=place):
                self.assertFalse(self.dash._names_a_trade(place))

    def test_a_trade_and_a_place_is(self):
        for q in ("plumbers in Riverside, CA", "roofers in Atlanta",
                  "HVAC in Compton, CA"):
            with self.subTest(query=q):
                self.assertTrue(self.dash._names_a_trade(q))

    def test_a_trade_on_its_own_is(self):
        self.assertTrue(self.dash._names_a_trade("plumbers"))


class TheSearchBoxTest(unittest.TestCase):
    """What the box does now: a place sends JARVIS hunting, and a search that
    finds nothing sends him anyway rather than leaving an empty result."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-box-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import dashboard_app as dash
        import solo_studio_agent as core
        self.dash, self.core = dash, core
        cfg = core.load_config()
        cfg["google_places_api_key"] = "k"
        core.save_config(cfg)
        dash.STATE.reload()
        dash.JOB.update(running="", label="", summary="")
        self.client = dash.app.test_client()
        self.hunted = []
        self.real = dash._start_hunt
        dash._start_hunt = lambda area: (self.hunted.append(area), True)[1]

    def tearDown(self):
        self.dash._start_hunt = self.real
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def post(self, query):
        return self.client.post("/action/find_leads", data={"query": query},
                                environ_base={"REMOTE_ADDR": "127.0.0.1"})

    def test_typing_a_city_sends_him_hunting_instead_of_a_doomed_search(self):
        self.post("Los Angeles, CA")
        self.assertEqual(self.hunted, ["Los Angeles, CA"])

    def test_a_search_that_finds_nothing_sends_him_hunting_too(self):
        self.dash.STATE.agent.find_leads = lambda q: {
            "found": 0, "added": 0, "seen": 20, "with_site": 20,
            "closed": 0, "social_only": 0}
        self.post("plumbers in Los Angeles, CA")
        self.assertEqual(self.hunted, ["Los Angeles, CA"])

    def test_a_search_that_works_is_left_alone(self):
        self.dash.STATE.agent.find_leads = lambda q: {
            "found": 3, "added": 3, "seen": 20, "with_site": 17,
            "closed": 0, "social_only": 1}
        self.post("plumbers in Los Angeles, CA")
        self.assertEqual(self.hunted, [], "no need to hunt — it found some")


if __name__ == "__main__":
    unittest.main(verbosity=2)
