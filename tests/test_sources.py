"""The four places JARVIS looks for leads.

Google's text search ranks by fame, which is nearly a definition of "has a
website". These are the other three, and the rules they all share: same lead
shape, same website check, and one being down never stops the others.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import solo_studio_agent as core   # noqa: E402


def resp(status=200, payload=None, text=""):
    return type("R", (), {
        "status_code": status,
        "text": text or json.dumps(payload or {}),
        "json": lambda self: payload or {},
    })()


def no_site_check(url, timeout=None):
    """Never touch the real internet from a test."""
    if not url:
        return core.SITE_NONE, ""
    platform = core.social_platform(url)
    return (core.SITE_SOCIAL, platform) if platform else (core.SITE_OK, "")


class NearbyByDistanceTest(unittest.TestCase):
    """The one that matters most: nearest, not best known."""

    def setUp(self):
        self.svc = core.Services({"google_places_api_key": "k"})
        self.calls = []

    def _post(self, payload, status=200):
        def fake(url, headers=None, json=None, timeout=None):
            self.calls.append((url, json))
            return resp(status, payload, text="bad type" if status == 400 else "")
        return fake

    def run_search(self, payload, status=200, **kw):
        with mock.patch.object(core.requests, "post",
                               self._post(payload, status)), \
                mock.patch.object(core, "check_website", no_site_check):
            return self.svc.places_nearby(34.05, -118.24, **kw)

    def test_it_asks_google_to_rank_by_distance(self):
        self.run_search({"places": []})
        _, body = self.calls[0]
        self.assertEqual(body["rankPreference"], "DISTANCE")
        self.assertIn("circle", body["locationRestriction"])

    def test_it_hits_the_nearby_endpoint_not_the_text_one(self):
        self.run_search({"places": []})
        self.assertTrue(self.calls[0][0].endswith("places:searchNearby"))

    def test_a_business_with_no_website_becomes_a_lead(self):
        got = self.run_search({"places": [
            {"id": "a", "displayName": {"text": "Joe Plumbing"},
             "formattedAddress": "1 Main St", "nationalPhoneNumber": "555",
             "businessStatus": "OPERATIONAL"}]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "Joe Plumbing")

    def test_closed_businesses_never_come_back(self):
        got = self.run_search({"places": [
            {"id": "a", "displayName": {"text": "Gone"},
             "businessStatus": "CLOSED_PERMANENTLY"}]})
        self.assertEqual(len(got), 0)

    def test_a_rejected_type_name_retries_without_the_filter(self):
        """A wrong type would silently return nothing, which is worse than a
        broader search. It must not fail closed."""
        seen = []

        def fake(url, headers=None, json=None, timeout=None):
            seen.append(json)
            if "includedTypes" in json:
                return resp(400, {}, text="Invalid includedTypes")
            return resp(200, {"places": [
                {"id": "a", "displayName": {"text": "Joe"},
                 "businessStatus": "OPERATIONAL"}]})

        with mock.patch.object(core.requests, "post", fake), \
                mock.patch.object(core, "check_website", no_site_check):
            got = self.svc.places_nearby(34.05, -118.24, types=["not_a_type"])
        self.assertEqual(len(seen), 2)
        self.assertNotIn("includedTypes", seen[1])
        self.assertEqual(len(got), 1)

    def test_a_real_error_is_not_swallowed(self):
        with self.assertRaises(core.ServiceError):
            self.run_search({}, status=500)

    def test_every_call_goes_through_the_money_meter(self):
        spent = []
        self.svc.meter = lambda: spent.append(1)
        self.run_search({"places": []})
        self.assertEqual(len(spent), 1)


class OpenStreetMapTest(unittest.TestCase):
    """Free, no key, and a different map of the world from Google's."""

    # The real Overpass JSON shape: nodes carry lat/lon, ways carry a center.
    PAYLOAD = {"elements": [
        {"type": "node", "id": 1, "lat": 41.7, "lon": -74.3,
         "tags": {"name": "Ann's Plumbing", "craft": "plumber",
                  "phone": "845-555-0100", "addr:housenumber": "12",
                  "addr:street": "Main St", "addr:city": "Ellenville"}},
        {"type": "way", "id": 2, "center": {"lat": 41.7, "lon": -74.3},
         "tags": {"name": "Ridge Auto", "shop": "car_repair",
                  "website": "https://ridgeauto.com"}},
        {"type": "node", "id": 3, "lat": 41.7, "lon": -74.3,
         "tags": {"amenity": "cafe"}},          # no name: not a business to write to
    ]}

    def setUp(self):
        self.svc = core.Services({})
        self.posts = []

    def _search(self, payload=None, statuses=(200,)):
        codes = list(statuses)

        def fake(url, data=None, timeout=None, headers=None):
            self.posts.append((url, data))
            code = codes.pop(0) if codes else 200
            return resp(code, payload if payload is not None else self.PAYLOAD)

        with mock.patch.object(core.requests, "post", fake), \
                mock.patch.object(core, "check_website", no_site_check):
            return self.svc.osm_nearby(41.7, -74.3)

    def test_it_reads_the_real_overpass_shape(self):
        got = self._search()
        names = {lead["name"] for lead in got}
        self.assertIn("Ann's Plumbing", names)
        self.assertNotIn("Ridge Auto", names)      # has a working website

    def test_it_keeps_the_phone_number_and_address(self):
        lead = [x for x in self._search() if x["name"] == "Ann's Plumbing"][0]
        self.assertEqual(lead["phone"], "845-555-0100")
        self.assertIn("Main St", lead["address"])

    def test_unnamed_map_points_are_not_businesses(self):
        for lead in self._search():
            self.assertTrue(lead["name"])

    def test_ids_cannot_collide_with_google_ids(self):
        for lead in self._search():
            self.assertTrue(lead["place_id"].startswith("osm:"))

    def test_the_query_asks_for_what_it_needs(self):
        self._search()
        query = self.posts[0][1]["data"]
        self.assertIn("[out:json]", query)
        self.assertIn("around:", query)
        self.assertIn("out center", query)

    def test_a_busy_mirror_falls_through_to_the_next(self):
        """It's a free volunteer service. Busy is normal, not broken."""
        got = self._search(statuses=(429, 200))
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(got)

    def test_all_mirrors_busy_says_so_plainly(self):
        with self.assertRaises(core.ServiceError) as caught:
            self._search(statuses=(429, 503, 504))
        self.assertIn("volunteers", str(caught.exception))

    def test_it_never_spends_google_money(self):
        spent = []
        self.svc.meter = lambda: spent.append(1)
        self._search()
        self.assertEqual(spent, [], "OpenStreetMap is free")


class YelpTest(unittest.TestCase):
    """Extra coverage, honestly labelled."""

    PAYLOAD = {"businesses": [
        {"id": "abc", "name": "Ray's Roofing", "phone": "+13105550100",
         "display_phone": "(310) 555-0100", "is_closed": False,
         "url": "https://www.yelp.com/biz/rays-roofing",
         "categories": [{"title": "Roofing"}],
         "location": {"display_address": ["1 Main St", "Inglewood, CA"]}},
        {"id": "closed", "name": "Shut Co", "is_closed": True,
         "url": "https://www.yelp.com/biz/shut"},
    ]}

    def setUp(self):
        self.svc = core.Services({"yelp_api_key": "yk"})

    def _search(self, status=200, payload=None):
        def fake(url, params=None, timeout=None, headers=None):
            self.headers = headers
            return resp(status, payload if payload is not None else self.PAYLOAD)
        with mock.patch.object(core.requests, "get", fake), \
                mock.patch.object(core, "check_website", no_site_check):
            return self.svc.yelp_nearby(34.05, -118.24)

    def test_a_yelp_only_business_is_a_lead(self):
        got = self._search()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "Ray's Roofing")

    def test_it_is_labelled_as_a_directory_page_not_a_checked_site(self):
        """Yelp never gives us their own website, so we must not pretend we
        looked at one."""
        self.assertEqual(self._search()[0]["site_status"], core.SITE_SOCIAL)

    def test_closed_businesses_are_skipped(self):
        self.assertEqual(len(self._search()), 1)

    def test_it_sends_the_key_as_a_bearer_token(self):
        self._search()
        self.assertEqual(self.headers["Authorization"], "Bearer yk")

    def test_no_key_says_it_is_optional(self):
        self.svc.config["yelp_api_key"] = ""
        with self.assertRaises(core.ServiceError) as caught:
            self.svc.yelp_nearby(1, 1)
        self.assertIn("optional", str(caught.exception))

    def test_a_bad_key_says_so(self):
        with self.assertRaises(core.ServiceError) as caught:
            self._search(status=401)
        self.assertIn("key", str(caught.exception))


class HunterTest(unittest.TestCase):
    """Addresses behind a domain we already know."""

    PAYLOAD = {"data": {"domain": "goneroofing.com", "emails": [
        {"value": "ray@goneroofing.com", "type": "personal", "confidence": 80},
        {"value": "info@goneroofing.com", "type": "generic", "confidence": 90},
    ]}}

    def setUp(self):
        self.svc = core.Services({"hunter_api_key": "hk"})

    def _find(self, status=200, payload=None):
        def fake(url, params=None, timeout=None):
            self.params = params
            return resp(status, payload if payload is not None else self.PAYLOAD)
        with mock.patch.object(core.requests, "get", fake):
            return self.svc.hunter_email("goneroofing.com")

    def test_it_prefers_the_business_address_over_a_persons(self):
        self.assertEqual(self._find()["email"], "info@goneroofing.com")

    def test_nothing_found_is_not_an_error(self):
        r = self._find(payload={"data": {"emails": []}})
        self.assertFalse(r["found"])

    def test_a_bad_key_says_so(self):
        with self.assertRaises(core.ServiceError) as caught:
            self._find(status=401)
        self.assertIn("key", str(caught.exception))


class WhichEmailFinderTest(unittest.TestCase):
    """Hunter needs a domain. Businesses with no website haven't got one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-finder-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.cfg = dict(core.DEFAULT_CONFIG, hunter_api_key="hk")
        self.svc = core.Services(self.cfg)
        self.agent = core.Agent(core.Database(), self.svc, self.cfg)
        self.used = []
        # Every route stubbed: a test must never reach the real internet, and
        # the paid one must never reach the real API.
        self.scraped = ""
        self.svc.scrape_email = lambda url: (
            self.used.append("scrape"),
            (self.scraped, url) if self.scraped else ("", ""))[1]
        self.svc.hunter_email = lambda d: (self.used.append("hunter"),
                                           {"found": True, "email": "a@b.com"})[1]
        self.svc.research_email = lambda lead: (self.used.append("claude"),
                                                {"found": False})[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def test_the_free_read_is_tried_before_anything_that_costs(self):
        """A model with web search runs about a dime a lead. Reading the page
        we already have the link to costs nothing, so it goes first."""
        self.scraped = "info@joesplumbing.com"
        r = self.agent._find_email({"id": 1,
                                    "social_url": "https://joesplumbing.com"})
        self.assertEqual(self.used, ["scrape"])
        self.assertEqual(r["email"], "info@joesplumbing.com")

    def test_a_dead_site_gives_hunter_a_domain_to_work_with(self):
        self.agent._find_email({"id": 1, "social_url": "https://goneroofing.com"})
        self.assertEqual(self.used, ["scrape", "hunter"])

    def test_no_website_at_all_goes_to_claude(self):
        self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(self.used, ["claude"])

    def test_a_facebook_page_is_not_a_domain_to_look_up(self):
        self.agent._find_email({"id": 1,
                                "social_url": "https://facebook.com/joes"})
        self.assertEqual(self.used, ["scrape", "claude"])

    def test_without_a_hunter_key_everything_goes_to_claude(self):
        self.cfg["hunter_api_key"] = ""
        self.agent._find_email({"id": 1, "social_url": "https://gone.com"})
        self.assertEqual(self.used, ["scrape", "claude"])

    def test_hunter_failing_falls_back_rather_than_giving_up(self):
        def boom(domain):
            raise core.ServiceError("Hunter's monthly quota is used up.")
        self.svc.hunter_email = boom
        self.agent._find_email({"id": 1, "social_url": "https://gone.com"})
        self.assertEqual(self.used, ["scrape", "claude"])

    # -- the money -----------------------------------------------------------

    def test_paid_lookups_are_counted(self):
        for _ in range(3):
            self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(self.agent.paid_lookups_this_month(), 3)

    def test_it_stops_spending_at_the_monthly_cap(self):
        self.cfg["monthly_lookup_cap"] = 2
        for _ in range(4):
            self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(self.used.count("claude"), 2)

    def test_free_lookups_carry_on_after_the_cap(self):
        self.cfg["monthly_lookup_cap"] = 0
        self.scraped = "info@x.com"
        r = self.agent._find_email({"id": 1, "social_url": "https://x.com"})
        self.assertTrue(r["found"])

    def test_the_cap_says_what_happened_rather_than_going_quiet(self):
        self.cfg["monthly_lookup_cap"] = 0
        r = self.agent._find_email({"id": 1, "social_url": None})
        self.assertIn("used up", r["note"])

    def test_the_paid_lookup_uses_the_cheap_model(self):
        """Extraction, not reasoning. Opus costs five times the tokens."""
        self.assertTrue(core.RESEARCH_MODEL.startswith("claude-haiku"))
        self.assertEqual(core.DEFAULT_CONFIG["research_model"],
                         core.RESEARCH_MODEL)

    def test_the_search_tool_matches_the_model(self):
        """The newer variant is a 400 on Haiku, not a graceful fallback."""
        self.assertEqual(core._web_search_tool_for("claude-haiku-4-5"),
                         "web_search_20250305")
        self.assertEqual(core._web_search_tool_for("claude-opus-5"),
                         "web_search_20260209")


class OneSourceDownTest(unittest.TestCase):
    """A sweep uses every index there is. Any of them may be having a bad day."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-sweep-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.cfg = dict(core.DEFAULT_CONFIG, google_places_api_key="k")
        self.svc = core.Services(self.cfg)
        self.agent = core.Agent(core.Database(), self.svc, self.cfg)
        self.svc.places_geocode = lambda area: (41.7, -74.3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def _results(self, *names):
        out = core.SearchResults(
            {"place_id": n, "name": n, "address": "a", "phone": "p",
             "category": "c", "social_url": None, "site_status": "none",
             "site_note": None} for n in names)
        out.seen = len(names)
        return out

    def test_openstreetmap_still_works_when_google_is_down(self):
        def boom(*a, **k):
            raise core.ServiceError("Google Places error 500")
        self.svc.places_nearby = boom
        self.svc.osm_nearby = lambda *a, **k: self._results("Ann's Plumbing")
        r = self.agent.sweep_town("Ellenville, NY")
        self.assertEqual(r["added"], 1)
        self.assertTrue(any("Google" in n for n in r["notes"]))

    def test_google_still_works_when_openstreetmap_is_busy(self):
        self.svc.places_nearby = lambda *a, **k: self._results("Ray Roofing")
        def busy(*a, **k):
            raise core.ServiceError("Couldn't reach OpenStreetMap")
        self.svc.osm_nearby = busy
        self.assertEqual(self.agent.sweep_town("Ellenville, NY")["added"], 1)

    def test_yelp_only_runs_when_a_key_is_set(self):
        used = []
        self.svc.places_nearby = lambda *a, **k: self._results()
        self.svc.osm_nearby = lambda *a, **k: self._results()
        self.svc.yelp_nearby = lambda *a, **k: (used.append(1),
                                                self._results("Y"))[1]
        self.agent.sweep_town("Ellenville, NY")
        self.assertEqual(used, [])
        self.cfg["yelp_api_key"] = "yk"
        self.agent.sweep_town("Ellenville, NY")
        self.assertEqual(used, [1])

    def test_a_town_it_cannot_place_does_not_crash_the_sweep(self):
        self.svc.places_geocode = lambda area: None
        r = self.agent.sweep_town("Nowhere at all")
        self.assertEqual(r["added"], 0)
        self.assertTrue(r["notes"])

    def test_a_town_we_already_know_is_never_looked_up(self):
        looked = []
        self.svc.places_geocode = lambda area: (looked.append(area), (41.7, -74.3))[1]
        self.assertIsNotNone(core.city_point("Ellenville, NY"))
        self.agent.town_centre("Ellenville, NY")
        self.assertEqual(looked, [], "it ships with the app")

    def test_a_town_we_do_not_know_is_looked_up_once(self):
        looked = []
        self.svc.places_geocode = lambda area: (looked.append(area), (41.7, -74.3))[1]
        self.agent.town_centre("Little Nowhere, NY")
        self.agent.town_centre("Little Nowhere, NY")
        self.assertEqual(len(looked), 1, "a town does not move")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ZeroMeansZeroTest(unittest.TestCase):
    """A cap of nought means spend nothing, not "use the default"."""

    def test_a_zero_cap_is_kept(self):
        self.assertEqual(core.setting_int({"cap": 0}, "cap", 200), 0)

    def test_a_missing_setting_falls_back(self):
        self.assertEqual(core.setting_int({}, "cap", 200), 200)

    def test_a_blank_setting_falls_back(self):
        self.assertEqual(core.setting_int({"cap": ""}, "cap", 200), 200)

    def test_nonsense_falls_back_rather_than_crashing(self):
        self.assertEqual(core.setting_int({"cap": "lots"}, "cap", 200), 200)

    def test_a_real_number_is_used(self):
        self.assertEqual(core.setting_int({"cap": "50"}, "cap", 200), 50)


class SpendDialTest(unittest.TestCase):
    """One dial for what the app may spend on Claude.

    Asked for plainly: "I want the app to use the least amount of credits
    possible." So the defaults have to be frugal, and the expensive things
    have to be the ones that only run when money is coming in.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-spend-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.cfg = dict(core.DEFAULT_CONFIG, anthropic_api_key="k")
        self.agent = core.Agent(core.Database(), core.Services(self.cfg),
                                self.cfg)
        self.used = []
        self.agent.services.scrape_email = lambda url: ("", "")
        self.agent.services.research_email = lambda lead: (
            self.used.append(1), {"found": False})[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def test_it_ships_frugal(self):
        self.assertEqual(core.DEFAULT_CONFIG["spend_level"], "frugal")

    def test_off_never_spends_a_penny_on_lookups(self):
        self.cfg["spend_level"] = "off"
        for _ in range(5):
            self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(self.used, [])

    def test_frugal_stops_at_a_handful_a_day(self):
        self.cfg["spend_level"] = "frugal"
        for _ in range(40):
            self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(len(self.used),
                         core.SPEND_LEVELS["frugal"]["lookups_day"])

    def test_a_day_cap_exists_or_a_month_goes_in_an_hour(self):
        """The researcher runs every tick and there are 1,440 in a day."""
        for level in core.SPEND_LEVELS.values():
            with self.subTest(level=level["label"][:12]):
                self.assertLessEqual(level["lookups_day"],
                                     level["lookups_month"])

    def test_the_monthly_cap_still_wins(self):
        self.cfg.update(spend_level="normal", monthly_lookup_cap=2)
        for _ in range(6):
            self.agent._find_email({"id": 1, "social_url": None})
        self.assertEqual(len(self.used), 2)

    def test_every_level_has_a_ceiling_worth_knowing(self):
        for name, level in core.SPEND_LEVELS.items():
            with self.subTest(level=name):
                ceiling = level["lookups_month"] * core.LOOKUP_DOLLARS
                self.assertLess(ceiling, 10, "no level may be a surprise bill")

    # -- which model does what ----------------------------------------------

    def test_frugal_reads_replies_with_the_cheap_model(self):
        self.cfg["spend_level"] = "frugal"
        self.assertTrue(core.thinking_model(self.cfg).startswith("claude-haiku"))

    def test_normal_reads_replies_with_the_good_one(self):
        self.cfg["spend_level"] = "normal"
        self.assertEqual(core.thinking_model(self.cfg), core.MAIN_MODEL)

    def test_designing_a_site_is_never_downgraded(self):
        """It only runs once somebody has asked for one, and it is the thing
        being sold."""
        import inspect
        src = inspect.getsource(core.Services.generate_site_html)
        self.assertIn("anthropic_model", src)
        self.assertNotIn("thinking_model", src)

    def test_the_town_lookup_no_longer_pays_for_web_search(self):
        """Four searches on the big model to name towns a model already knows,
        each of which gets checked against the map anyway."""
        import inspect
        src = inspect.getsource(core.Services.towns_near)
        self.assertNotIn("web_search", src)
        self.assertIn("RESEARCH_MODEL", src)

    def test_reading_a_reply_cannot_run_away(self):
        """One word of output, so it is bounded — thinking included."""
        import inspect
        src = inspect.getsource(core.Services.classify_reply)
        self.assertIn("max_tokens=200", src)


class NeverStopsResearchingTest(unittest.TestCase):
    """A budget holding a lookup back is not the same as having answered it.

    Marking the lead researched anyway retired it for good over a cap that
    clears tomorrow — which is how the researching quietly stopped.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-retry-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.cfg = dict(core.DEFAULT_CONFIG, anthropic_api_key="k",
                        spend_level="off")     # every paid lookup refused
        self.svc = core.Services(self.cfg)
        self.agent = core.Agent(core.Database(), self.svc, self.cfg)
        self.svc.scrape_email = lambda url: ("", "")
        self.svc.research_email = lambda lead: {"found": True,
                                                "email": "a@b.com",
                                                "source": "s", "note": "n"}
        self.lead = self.agent.db.add_lead(place_id="p", name="Biz",
                                           address="a", phone="p", category="c")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def test_a_capped_lead_is_not_retired(self):
        self.agent.research_missing_emails(force=True)
        self.assertIsNone(self.agent.db.get_lead(self.lead)["researched_at"])

    def test_and_it_is_picked_up_once_the_budget_is_back(self):
        self.agent.research_missing_emails(force=True)
        self.assertEqual(len(self.agent.db.leads_to_research(5)), 1)
        self.cfg["spend_level"] = "normal"
        self.agent.research_missing_emails(force=True)
        self.assertEqual(self.agent.db.get_lead(self.lead)["email"], "a@b.com")

    def test_a_lead_that_was_answered_is_retired(self):
        """Not-found is an answer; it must not be asked forever."""
        self.cfg["spend_level"] = "normal"
        self.svc.research_email = lambda lead: {"found": False, "note": "none"}
        self.agent.research_missing_emails(force=True)
        self.assertIsNotNone(self.agent.db.get_lead(self.lead)["researched_at"])

    def test_the_free_routes_never_stop_whatever_the_budget(self):
        self.svc.scrape_email = lambda url: ("info@x.com", url)
        self.agent.db.update_lead(self.lead, social_url="https://x.com")
        self.agent.research_missing_emails(force=True)
        self.assertEqual(self.agent.db.get_lead(self.lead)["email"],
                         "info@x.com")
