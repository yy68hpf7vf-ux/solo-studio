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
        dash.HUNT.update(running=False, area="", summary="")
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
