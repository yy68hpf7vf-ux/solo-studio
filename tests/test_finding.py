"""Actually finding leads.

Written from a real failure: the user typed "Atlanta, Georgia", ran the hunt,
and got nothing at all — no leads, and no word about why.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def place(name, website=None, status="OPERATIONAL"):
    p = {"id": "id-" + name, "displayName": {"text": name},
         "formattedAddress": "1 Peachtree St, Atlanta, GA",
         "nationalPhoneNumber": "404-555-0100",
         "primaryTypeDisplayName": {"text": "Roofing contractor"},
         "businessStatus": status}
    if website:
        p["websiteUri"] = website
    return p


class _Resp:
    status_code = 200

    def __init__(self, places, token=None):
        self._body = {"places": places}
        if token:
            self._body["nextPageToken"] = token

    def json(self):
        return self._body


class WhatCountsAsNoWebsiteTest(unittest.TestCase):
    """A Facebook page in the website slot is the pitch, not a disqualifier."""

    def setUp(self):
        import solo_studio_agent as core
        self.core = core
        self.svc = core.Services({"google_places_api_key": "k"})

    def _search(self, places, verdicts=None):
        """Run a search with the website check stubbed.

        Never let a test reach the real internet: it would be slow, flaky, and
        would quietly turn every made-up domain into a "dead site" lead.
        """
        table = verdicts or {}

        def fake_check(url, timeout=None):
            if not url:
                return self.core.SITE_NONE, ""
            if url in table:
                return table[url]
            platform = self.core.social_platform(url)
            if platform:
                return self.core.SITE_SOCIAL, platform
            return self.core.SITE_OK, ""

        with mock.patch.object(self.core.requests, "post",
                               return_value=_Resp(places)), \
                mock.patch.object(self.core, "check_website", fake_check):
            return self.svc.places_search_no_website("roofers in Atlanta, GA")

    def test_a_working_website_disqualifies_them(self):
        got = self._search([place("Big Roofing", "https://bigroofing.com")])
        self.assertEqual(len(got), 0)
        self.assertEqual(got.with_site, 1)

    def test_no_website_at_all_is_the_lead(self):
        got = self._search([place("Blank Co")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["site_status"], self.core.SITE_NONE)

    def test_a_facebook_page_counts_as_no_website(self):
        """There is nowhere of their own to send a customer, which is the
        pitch — and somebody there already tried, which makes it warmer than
        a blank listing."""
        got = self._search([place("Joe Roofs", "https://facebook.com/joeroofs")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["site_status"], self.core.SITE_SOCIAL)

    def test_a_site_that_merely_does_not_load_is_no_longer_a_lead(self):
        """They have a website. It is a bad one, which is a different and
        weaker conversation — the widths that covered it were taken out."""
        got = self._search(
            [place("Gone Roofing", "https://goneroofing.com")],
            {"https://goneroofing.com": (self.core.SITE_DEAD, "HTTP 404")})
        self.assertEqual(len(got), 0)

    def test_nor_is_a_parked_domain_or_an_old_site(self):
        for status in (self.core.SITE_PARKED, self.core.SITE_INSECURE,
                       self.core.SITE_NOT_MOBILE):
            with self.subTest(status=status):
                got = self._search([place("Co", "https://co.com")],
                                   {"https://co.com": (status, "x")})
                self.assertEqual(len(got), 0)

    def test_there_is_only_one_bar_now(self):
        self.assertEqual(set(self.core.LEAD_STATUSES),
                         {self.core.SITE_NONE, self.core.SITE_SOCIAL})

    def test_closed_businesses_are_left_alone(self):
        got = self._search([place("Gone", None, "CLOSED_PERMANENTLY")])
        self.assertEqual(len(got), 0)
        self.assertEqual(got.closed, 1)

    def test_it_counts_everything_it_looked_at(self):
        got = self._search([place("A", "https://a.com"), place("B"),
                            place("C", "https://instagram.com/c"),
                            place("D", None, "CLOSED_TEMPORARILY")])
        self.assertEqual((got.seen, got.with_site, got.social_only, got.closed),
                         (4, 1, 1, 1))
        self.assertEqual(len(got), 2)
        self.assertEqual(got.by_status,
                         {self.core.SITE_NONE: 1, self.core.SITE_SOCIAL: 1})


class SayingWhyNothingWasFoundTest(unittest.TestCase):
    """The complaint was 'it didn't find any leads', and the app's answer was
    silence. Every outcome has to explain itself."""

    def setUp(self):
        import solo_studio_agent as core
        self.core = core

    def test_a_city_where_everyone_has_a_site_says_so_and_says_what_to_do(self):
        said = self.core.describe_search(
            {"seen": 60, "with_site": 60, "found": 0, "added": 0})
        self.assertIn("60", said)
        self.assertIn("smaller towns", said)

    def test_nothing_at_all_from_google_reads_as_a_typo_not_a_dry_area(self):
        said = self.core.describe_search({"seen": 0, "found": 0, "added": 0})
        self.assertIn("spelling", said)

    def test_already_in_the_list_is_not_the_same_as_nothing_there(self):
        said = self.core.describe_search(
            {"seen": 20, "with_site": 14, "found": 6, "added": 0})
        self.assertIn("already in your list", said)

    def test_a_good_run_leads_with_the_number_that_matters(self):
        said = self.core.describe_search(
            {"seen": 60, "with_site": 52, "found": 8, "added": 5,
             "by_status": {"none": 3, "dead": 3, "social": 2}})
        self.assertIn("5 new", said)

    def test_it_says_why_each_one_is_worth_pitching(self):
        said = self.core.describe_search(
            {"seen": 60, "with_site": 52, "found": 8, "added": 5,
             "by_status": {"dead": 5, "social": 3}})
        self.assertIn("doesn't load", said)
        self.assertIn("social page", said)


class SpreadingTheSearchTest(unittest.TestCase):
    """One run has a fixed budget of searches. Spending all of it inside the
    one city the user typed is why Atlanta came back empty."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-find-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        import solo_studio_agent as core
        import dashboard_app as dash
        cls.core, cls.dash = core, dash
        dash.STATE.reload()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def _build(self, towns, trades):
        cfg = self.core.load_config()
        cfg["anthropic_api_key"] = "sk-ant-test"
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        with mock.patch.object(self.core.Services, "towns_near",
                               return_value=towns):
            self.dash.app.test_client().post(
                "/action/build_searches",
                data={"territory_base": "Atlanta, GA", "territory_miles": "30",
                      "trades": "\n".join(trades)},
                environ_base={"REMOTE_ADDR": "127.0.0.1"})
        return [q for q in self.core.load_config()["saved_searches"].splitlines()
                if q.strip()]

    def test_the_first_run_does_not_sit_in_one_town(self):
        towns = ["Atlanta, GA", "Decatur, GA", "Marietta, GA", "Smyrna, GA",
                 "Hapeville, GA", "Forest Park, GA"]
        trades = ["roofers", "plumbers", "electricians", "landscapers"]
        lines = self._build(towns, trades)
        first_run = lines[:20]
        towns_hit = {q.split(" in ", 1)[1] for q in first_run}
        self.assertGreaterEqual(
            len(towns_hit), len(towns),
            "a run's budget must be spread across towns, not spent on one")

    def test_every_trade_and_town_pair_is_still_searched_exactly_once(self):
        towns = ["Atlanta, GA", "Decatur, GA", "Marietta, GA"]
        trades = ["roofers", "plumbers", "electricians", "landscapers"]
        lines = self._build(towns, trades)
        self.assertEqual(len(lines), len(towns) * len(trades))
        self.assertEqual(len(set(lines)), len(lines))


class WhichTownsToAskForTest(unittest.TestCase):
    """The town list is where the whole hunt succeeds or fails."""

    def test_it_asks_for_small_places_first(self):
        """It used to ask for the biggest and best-known first, which is
        precisely backwards: the city centre is where every business already
        has a website, and a run's budget got spent there first."""
        import solo_studio_agent as core

        class _Client:
            def __init__(self):
                self.messages = self

            def create(self, **kw):
                self.kw = kw
                return type("M", (), {
                    "stop_reason": "end_turn",
                    "content": [type("B", (), {"type": "text",
                                               "text": "Hapeville, GA"})()]})()

        svc = core.Services({"anthropic_api_key": "k"})
        client = _Client()
        with mock.patch.object(core.Services, "_get_anthropic",
                               return_value=client):
            svc.towns_near("Atlanta, GA", 30)
        prompt = client.kw["messages"][0]["content"].lower()
        self.assertIn("smallest first", prompt)
        self.assertNotIn("biggest and best-known first", prompt)
        self.assertIn("suburbs", prompt)


class ARunAlwaysReportsTest(unittest.TestCase):
    """A run that finds nothing wrote nothing to the log, so 'it didn't find
    any leads' had no answer anywhere in the app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-run-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        import solo_studio_agent as core
        import dashboard_app as dash
        cls.core, cls.dash = core, dash
        dash.STATE.reload()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def _run(self, result):
        cfg = self.core.load_config()
        cfg.update(saved_searches="roofers in Atlanta, GA\nplumbers in Decatur, GA",
                   searches_per_run=2, auto_search_enabled=True)
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        agent = self.dash.STATE.agent
        agent.find_leads = lambda q: dict(result)
        return agent.run_saved_searches(force=True)

    def _last_log(self):
        for ev in self.dash.STATE.db.recent_events(20):
            if ev["kind"] == "auto_search":
                return ev["detail"]
        return ""

    def test_a_run_that_finds_nothing_still_says_what_it_did(self):
        """Two searches, 60 businesses each: the run reports the total it
        looked at, not silence."""
        r = self._run({"added": 0, "seen": 60, "found": 0})
        self.assertIn("120 businesses", r["summary"])
        self.assertIn("120 businesses", self._last_log())
        self.assertIn("had a website", self._last_log())

    def test_it_distinguishes_a_dry_area_from_a_known_one(self):
        self._run({"added": 0, "seen": 40, "found": 7})
        self.assertIn("already in your list", self._last_log())

    def test_a_good_run_says_how_many_are_new(self):
        self._run({"added": 3, "seen": 40, "found": 5})
        self.assertIn("6 new leads", self._last_log())   # 3 per search, two searches


class BudgetStaysFreeTest(unittest.TestCase):
    def test_the_default_run_size_stays_inside_googles_free_allowance(self):
        import dashboard_app as dash
        import solo_studio_agent as core
        cost = dash._search_cost(
            {"saved_searches": "\n".join("q%d" % i for i in range(500)),
             "searches_per_run": core.DEFAULT_CONFIG["searches_per_run"],
             "search_interval_hours": 12})
        self.assertFalse(cost["over"],
                         f"{cost['calls_month']} calls a month would bill the user")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ApproveFiltersTest(unittest.TestCase):
    """Two filters over one page: how good the lead is, and whether it has an
    address yet. Both narrow the view; neither throws anything away."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-filters-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import dashboard_app as dash
        import solo_studio_agent as core
        self.dash, self.core = dash, core
        dash.STATE.reload()
        db = dash.STATE.db
        # ready to send, no website at all
        db.add_lead(place_id="a", name="Blank Co", address="a", phone="p",
                    category="c", email="a@b.com", site_status="none")
        # ready to send, only a dead site
        db.add_lead(place_id="b", name="Dead Co", address="a", phone="p",
                    category="c", email="b@b.com", site_status="dead")
        # no address yet
        db.add_lead(place_id="c", name="Nameless Co", address="a", phone="p",
                    category="c", site_status="none")
        self.client = dash.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def page(self, query=""):
        return self.client.get("/approve" + query,
                               environ_base={"REMOTE_ADDR": "127.0.0.1"}
                               ).data.decode()

    def test_everything_shows_by_default(self):
        html = self.page()
        for name in ("Blank Co", "Dead Co", "Nameless Co"):
            self.assertIn(name, html)

    def test_needs_an_email_hides_the_ones_ready_to_send(self):
        html = self.page("?have=no")
        self.assertIn("Nameless Co", html)
        self.assertNotIn("Blank Co", html)

    def test_ready_to_send_hides_the_ones_without_an_address(self):
        html = self.page("?have=yes")
        self.assertIn("Blank Co", html)
        self.assertNotIn("Nameless Co", html)

    def test_the_counts_are_real(self):
        html = self.page()
        self.assertIn("Ready to send (2)", html)
        self.assertIn("Needs an email (1)", html)

    def test_filtering_never_deletes_anything(self):
        """A filter hides; it must not remove. Counted as a difference, since
        the shared database may hold rows from elsewhere in the suite."""
        before = len(self.dash.STATE.db.all_leads())
        self.page("?have=no")
        self.page("?have=yes")
        self.assertEqual(len(self.dash.STATE.db.all_leads()), before)


class NotABusinessTest(unittest.TestCase):
    """"It was giving me some sheriff office stuff."

    A map search returns everything on the map. A sheriff's office has no
    website to sell and nobody to sell it to.
    """

    def setUp(self):
        import solo_studio_agent as core
        self.core = core

    def dropped(self, name, category=""):
        return not self.core.is_a_business(name, category)

    def test_a_sheriffs_office_is_not_a_lead(self):
        self.assertTrue(self.dropped("El Paso County Sheriff's Office",
                                     "Police department"))

    def test_nor_are_schools_churches_libraries_or_the_city(self):
        for name, cat in (("Ysleta High School", "School"),
                          ("First Baptist Church", "Church"),
                          ("El Paso Public Library", "Library"),
                          ("City of El Paso Water", "Government office"),
                          ("Sunland Park Fire Department", "Fire station"),
                          ("Downtown Post Office", "Post office")):
            with self.subTest(name=name):
                self.assertTrue(self.dropped(name, cat))

    def test_they_are_caught_without_a_category_too(self):
        """OpenStreetMap often has a name and little else."""
        for name in ("Ysleta High School", "City of El Paso Water",
                     "El Paso County Sheriff", "Hudson School District"):
            with self.subTest(name=name):
                self.assertTrue(self.dropped(name))

    def test_a_real_business_with_an_awkward_name_is_kept(self):
        """The trap in the other direction, and the more expensive one: these
        are the leads, and a blunt word match throws them away."""
        for name, cat in (("Church Street Auto Repair", "Car repair"),
                          ("Church Street Auto Repair", ""),
                          ("Trinity Church Landscaping", ""),
                          ("School Street Barbers", "Barber shop"),
                          ("Old Jail Brewing Company", "Brewery"),
                          ("Courthouse Coffee", "Coffee shop"),
                          ("Town Hall Tavern", "Bar"),
                          ("The Old Post Office Cafe", "Cafe")):
            with self.subTest(name=name):
                self.assertFalse(self.dropped(name, cat))

    def test_ordinary_trades_are_never_touched(self):
        for name, cat in (("Joe's Plumbing", "Plumber"),
                          ("Sunrise Landscaping", "Landscaper"),
                          ("Ray's Roofing", "Roofing contractor")):
            with self.subTest(name=name):
                self.assertFalse(self.dropped(name, cat))

    def test_the_filter_runs_on_every_source(self):
        """Google text, Google nearby, OpenStreetMap and Yelp all land in the
        same place, so the check belongs there."""
        import inspect
        src = inspect.getsource(self.core.Services._triage)
        self.assertIn("is_a_business", src)
