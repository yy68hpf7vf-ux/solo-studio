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

    def test_the_crawl_is_what_runs_every_round_now(self):
        """Topping up to fifteen leads was the cap. The crawl covers the map
        instead, so this is the one that has to be in the round."""
        import inspect
        self.assertIn("self.crawl", inspect.getsource(self.core.Agent.tick))


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


class CrawlTest(unittest.TestCase):
    """Covering the country on its own, city by city.

    The complaint this answers: "I typed el paso and it didn't find one thing.
    I told you to make it automatic without me clicking anything — JARVIS
    should search city by city, state by state."
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-crawl-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k",
                        auto_search_enabled=True,
                        territory_base="El Paso, TX", tiles_per_tick=2)
        self.agent = core.Agent(core.Database(), core.Services(self.cfg),
                                self.cfg)
        self.agent.services.towns_near = lambda base, miles: ["Socorro, TX"]
        self.placed = []
        self.agent.services.places_geocode = lambda area: (
            self.placed.append(area), (31.76 + len(self.placed) * 0.01, -106.49))[1]
        self.swept = []
        self.agent.sweep_point = lambda label, lat, lng, radius=0: (
            self.swept.append((label, lat, lng)), {"added": 1, "seen": 5})[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    # -- the queue -----------------------------------------------------------

    def test_it_queues_the_whole_country(self):
        cities = self.agent.crawl_cities()
        self.assertGreater(len(cities), 1000)

    def test_home_comes_first_then_the_towns_round_it(self):
        cities = self.agent.crawl_cities()
        self.assertEqual(cities[0], "El Paso, TX")
        self.assertEqual(cities[1], "Socorro, TX")

    def test_their_own_state_comes_before_anywhere_else(self):
        cities = self.agent.crawl_cities()
        states = [c.rsplit(", ", 1)[-1] for c in cities[:60]]
        self.assertEqual(set(states), {"TX"})

    def test_no_city_is_queued_twice(self):
        cities = self.agent.crawl_cities()
        lowered = [c.lower() for c in cities]
        self.assertEqual(len(set(lowered)), len(lowered))

    def test_the_queue_is_built_once_and_remembered(self):
        asked = []
        self.agent.services.towns_near = lambda base, miles: (
            asked.append(base), ["Socorro, TX"])[1]
        self.agent.crawl_cities()
        self.agent.crawl_cities()
        self.assertEqual(len(asked), 1)

    def test_moving_home_rebuilds_the_queue(self):
        self.agent.services.towns_near = lambda base, miles: (
            ["Socorro, TX"] if "El Paso" in base else ["Troy, NY"])
        self.agent.crawl_cities()
        self.cfg["territory_base"] = "Albany, NY"
        cities = self.agent.crawl_cities()
        self.assertEqual(cities[:2], ["Albany, NY", "Troy, NY"])
        self.assertEqual(int(self.agent.db.get_kv("crawl_city")), 0)

    def test_with_no_home_town_it_uses_the_address_they_already_gave(self):
        """"Without me clicking anything" has to mean exactly that."""
        self.cfg["territory_base"] = ""
        self.cfg["mailing_address"] = "12 Main St, Ellenville, NY 12428"
        self.assertEqual(self.agent.crawl_cities()[0], "Ellenville, NY")

    def test_with_nothing_at_all_it_still_crawls_the_country(self):
        """Knowing nothing about them is not a reason to do nothing."""
        self.cfg["territory_base"] = ""
        self.cfg["mailing_address"] = ""
        cities = self.agent.crawl_cities()
        self.assertGreater(len(cities), 1000)
        self.assertFalse(self.agent.crawl().get("skipped"))

    def test_it_carries_on_without_the_town_list(self):
        """Out of Claude credit must not stop the country crawl."""
        def boom(base, miles):
            raise self.core.ServiceError("$0 of API credit")
        self.agent.services.towns_near = boom
        cities = self.agent.crawl_cities()
        self.assertEqual(cities[0], "El Paso, TX")
        self.assertGreater(len(cities), 1000)

    # -- walking it ----------------------------------------------------------

    def test_it_sweeps_several_spots_across_a_city_not_one(self):
        """Google returns the twenty businesses nearest a point and no more,
        so one point per city is one street corner per city."""
        self.cfg["tiles_per_tick"] = 20
        self.agent.crawl()
        places = {(lat, lng) for _, lat, lng in self.swept}
        self.assertGreaterEqual(len(places), self.agent.TILE_GRID ** 2)

    def test_it_moves_on_to_the_next_city_when_one_is_done(self):
        self.cfg["tiles_per_tick"] = self.agent.TILE_GRID ** 2 + 2
        self.agent.crawl()
        self.assertIn("El Paso, TX", [label for label, _, _ in self.swept])
        self.assertIn("Socorro, TX", [label for label, _, _ in self.swept])

    def test_it_never_sweeps_the_same_spot_twice(self):
        for _ in range(12):
            self.agent.crawl()
        self.assertEqual(len(set(self.swept)), len(self.swept))

    def test_it_picks_up_where_it_left_off_after_a_restart(self):
        self.agent.crawl()
        fresh = self.core.Agent(self.core.Database(),
                                self.core.Services(self.cfg), self.cfg)
        fresh.services.towns_near = self.agent.services.towns_near
        fresh.services.places_geocode = self.agent.services.places_geocode
        seen = []
        fresh.sweep_point = lambda label, lat, lng, radius=0: (
            seen.append((label, lat, lng)), {"added": 0, "seen": 0})[1]
        fresh.crawl()
        self.assertFalse(set(seen) & set(self.swept), "it must not start over")

    def test_a_city_it_cannot_place_is_skipped_not_fatal(self):
        """A town nobody has heard of: not in the built-in list, and the
        lookup comes back empty."""
        self.cfg["territory_base"] = "Nowheresville, ZZ"
        self.agent.services.towns_near = lambda base, miles: []
        self.agent.services.places_geocode = lambda area: None
        r = self.agent.crawl()
        # It steps over the one it can't place and carries on with the next,
        # rather than stalling on it.
        self.assertGreater(int(self.agent.db.get_kv("crawl_city")), 0)
        self.assertTrue(self.swept, "an unplaceable city must not stop the crawl")
        self.assertNotIn("Nowheresville, ZZ",
                         [label for label, _, _ in self.swept])

    def test_a_known_city_costs_no_lookup_at_all(self):
        """1,321 of them ship with the app, so the crawl doesn't buy a Google
        call just to learn where El Paso is."""
        self.cfg["tiles_per_tick"] = 4
        self.agent.crawl()
        self.agent.crawl()
        self.assertEqual(self.placed, [])

    def test_an_unknown_town_is_still_looked_up_once(self):
        self.assertIsNone(self.core.city_point("Napanoch, NY"))
        self.agent.town_centre("Napanoch, NY")
        self.agent.town_centre("Napanoch, NY")
        self.assertEqual(self.placed.count("Napanoch, NY"), 1)

    def test_a_failing_spot_does_not_stop_the_crawl(self):
        calls = []

        def flaky(label, lat, lng, radius=0):
            calls.append(label)
            if len(calls) == 1:
                raise self.core.ServiceError("Google said no")
            return {"added": 1, "seen": 5}
        self.agent.sweep_point = flaky
        self.agent.crawl()
        self.assertEqual(len(calls), 2)

    def test_it_stops_at_the_target_rather_than_hoarding(self):
        self.cfg["lead_target"] = 3
        for i in range(4):
            self.agent.db.add_lead(place_id=f"p{i}", name=f"Biz {i}",
                                   address="a", phone="p", category="c")
        self.assertIn("target", self.agent.crawl()["skipped"])
        self.assertEqual(self.swept, [])

    def test_switching_automatic_hunting_off_stops_it(self):
        self.cfg["auto_search_enabled"] = False
        self.agent.crawl()
        self.assertEqual(self.swept, [])

    def test_the_target_is_high_enough_to_be_worth_having(self):
        self.assertGreaterEqual(self.core.DEFAULT_CONFIG["lead_target"], 500)


class CityListTest(unittest.TestCase):
    """The list itself, since a wrong entry is a search that finds nothing."""

    def setUp(self):
        import solo_studio_agent as core
        self.core = core

    def test_every_state_is_covered(self):
        self.assertEqual(len(self.core.US_CITIES_BY_STATE), 51)  # 50 + DC

    def test_it_is_a_serious_list_not_a_token_one(self):
        self.assertGreater(len(self.core.us_cities()), 1000)

    def test_el_paso_is_in_it(self):
        """The one that started this."""
        self.assertIn("El Paso, TX", self.core.us_cities())

    def test_every_entry_reads_as_city_and_state(self):
        for city in self.core.us_cities():
            with self.subTest(city=city):
                self.assertRegex(city, r"^[A-Za-z][A-Za-z .'\-]*, [A-Z]{2}$")

    def test_the_home_state_can_be_put_first(self):
        self.assertTrue(self.core.us_cities("NM")[0].endswith(", NM"))

    def test_an_unknown_state_is_not_an_error(self):
        self.assertGreater(len(self.core.us_cities("ZZ")), 1000)

    def test_the_state_is_read_off_a_town_name(self):
        self.assertEqual(self.core._state_of("El Paso, TX"), "TX")
        self.assertEqual(self.core._state_of("Ellenville, NY 12428"), "NY")
        self.assertEqual(self.core._state_of("nowhere"), "")
        self.assertEqual(self.core._state_of("Springfield, Illinois"), "")


class NothingToClickTest(unittest.TestCase):
    """JARVIS finding leads and looking up addresses on his own.

    Asked for repeatedly, and still not happening, because the whole round was
    gated on an Inkbox key — which finding leads has nothing to do with.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-auto-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k",
                        inkbox_api_key="", auto_search_enabled=True,
                        territory_base="El Paso, TX")
        self.agent = core.Agent(core.Database(), core.Services(self.cfg),
                                self.cfg)
        self.ran = []
        for name in ("process_replies", "poll_payments", "tick_transients",
                     "run_saved_searches", "crawl", "research_missing_emails"):
            setattr(self.agent, name,
                    (lambda n: lambda *a, **k: self.ran.append(n))(name))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def test_no_mailbox_does_not_stop_him_finding_leads(self):
        self.agent.tick()
        self.assertIn("crawl", self.ran)
        self.assertIn("research_missing_emails", self.ran)

    def test_reading_replies_is_the_only_part_that_needs_a_mailbox(self):
        self.agent.tick()
        self.assertNotIn("process_replies", self.ran)
        self.cfg["inkbox_api_key"] = "ib"
        self.ran.clear()
        self.agent.tick()
        self.assertIn("process_replies", self.ran)

    def test_one_step_failing_does_not_take_the_round_down(self):
        """A mailbox problem used to stop the crawling and the researching."""
        self.cfg["inkbox_api_key"] = "ib"

        def boom():
            self.ran.append("process_replies")
            raise self.core.ServiceError("mailbox unreachable")
        self.agent.process_replies = boom
        self.agent.tick()
        self.assertIn("crawl", self.ran)
        self.assertIn("research_missing_emails", self.ran)

    def test_a_failed_step_is_written_down_rather_than_swallowed(self):
        def boom():
            raise self.core.ServiceError("Google said no")
        self.agent.crawl = boom
        self.agent.tick()
        kinds = [e["kind"] for e in self.agent.db.recent_events(20)]
        self.assertIn("tick_failed", kinds)

    def test_the_background_loop_no_longer_asks_for_a_mailbox(self):
        import inspect
        import dashboard_app as dash
        src = inspect.getsource(dash._autopilot_loop)
        self.assertNotIn('cfg.get("inkbox_api_key")', src)
