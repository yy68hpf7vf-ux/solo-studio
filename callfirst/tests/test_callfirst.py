"""What this app must get right.

The app it replaces failed in a way no test caught, because the tests agreed
with the code's assumptions instead of checking them against the world. So
these lean on the two or three facts that decide whether this thing works at
all, and on the one rule that must never bend: no site goes out unpaid.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def place(name, phone="845-555-0100", website=None, status="OPERATIONAL"):
    p = {"id": "id-" + name, "displayName": {"text": name},
         "formattedAddress": "1 Main St, Napanoch, NY",
         "primaryTypeDisplayName": {"text": "Plumber"},
         "businessStatus": status,
         "googleMapsUri": "https://maps.google.com/?cid=" + name}
    if phone:
        p["nationalPhoneNumber"] = phone
    if website:
        p["websiteUri"] = website
    return p


class _Resp:
    status_code = 200

    def __init__(self, places):
        self._b = {"places": places}

    def json(self):
        return self._b


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="call-first-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import engine as core
        import app as dash
        self.core, self.dash = core, dash
        dash.STATE.reload()
        cfg = core.load_config()
        cfg.update(your_name="Sam", studio_name="Solo Studio",
                   site_price_usd=500, territory_base="Napanoch, NY",
                   trades="plumbers", google_places_api_key="k")
        core.save_config(cfg)
        dash.STATE.reload()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("SOLO_STUDIO_HOME", None)

    def find(self, places, verdicts=None):
        table = verdicts or {}

        def fake_check(url, timeout=None):
            if not url:
                return self.core.SITE_NONE, ""
            return table.get(url, (self.core.SITE_OK, ""))

        with mock.patch.object(self.core.requests, "post",
                               return_value=_Resp(places)), \
                mock.patch.object(self.core, "check_website", fake_check):
            return self.dash.STATE.agent.find_businesses()


class WhoGoesOnTheSheetTest(_Base):
    """The rule that makes this app different from the one it replaces.

    The old rule was "only businesses with no website". It made the better
    pitch and was unworkable: those businesses have no email address online
    either, so there was no way to reach them. The rule now is a phone number.
    """

    def test_a_business_with_no_website_is_on_the_sheet(self):
        self.find([place("Blank Co")])
        self.assertEqual([l["name"] for l in self.dash.STATE.db.leads_to_call()],
                         ["Blank Co"])

    def test_a_business_with_a_working_website_is_on_it_too(self):
        """This is the reversal. Their site being fine is not a reason not to
        ring them — it only changes what you open with."""
        self.find([place("Ace Drain", website="https://ace.example")])
        sheet = self.dash.STATE.db.leads_to_call()
        self.assertEqual([l["name"] for l in sheet], ["Ace Drain"])
        self.assertEqual(sheet[0]["site_status"], self.core.SITE_OK)

    def test_no_phone_number_means_no_lead(self):
        """The whole app is a phone call. A business you cannot ring is no use
        however good the pitch would have been."""
        self.find([place("Silent Co", phone=None)])
        self.assertEqual(self.dash.STATE.db.leads_to_call(), [])

    def test_a_closed_business_is_left_alone(self):
        self.find([place("Gone Co", status="CLOSED_PERMANENTLY")])
        self.assertEqual(self.dash.STATE.db.leads_to_call(), [])

    def test_the_listing_link_is_kept_so_you_can_look_while_it_rings(self):
        self.find([place("Blank Co")])
        lead = self.dash.STATE.db.leads_to_call()[0]
        self.assertIn("maps.google.com", lead["maps_url"])

    def test_a_school_is_not_a_business(self):
        self.find([place("Napanoch Elementary School")])
        self.assertEqual(self.dash.STATE.db.leads_to_call(), [])


class WhatToSayTest(_Base):
    """The opener has to match what you are looking at. Telling someone their
    website is broken when it isn't gets you caught in ten seconds."""

    def _say(self, status):
        return self.core.call_opener({"name": "Joe's", "site_status": status},
                                     self.dash.STATE.config)

    def test_no_website_says_so(self):
        self.assertIn("don't have a website", self._say(self.core.SITE_NONE))

    def test_a_dead_site_says_it_is_not_loading(self):
        self.assertIn("not loading", self._say(self.core.SITE_DEAD))

    def test_a_working_site_is_not_called_broken(self):
        said = self._say(self.core.SITE_OK)
        for lie in ("not loading", "don't have a website", "nothing on it"):
            self.assertNotIn(lie, said)

    def test_every_opener_ends_by_asking_for_the_email(self):
        """It is the only thing the call has to achieve. Everything after it
        is automatic, and none of it can start without an address."""
        for status in (self.core.SITE_NONE, self.core.SITE_DEAD,
                       self.core.SITE_SOCIAL, self.core.SITE_OK,
                       self.core.SITE_NOT_MOBILE):
            with self.subTest(status=status):
                self.assertIn("best email to send it to", self._say(status))


class SayingYesTest(_Base):
    """The pivot: a yes on the phone and an address read out loud."""

    def setUp(self):
        super().setUp()
        self.find([place("Rivera Plumbing")])
        self.lead = self.dash.STATE.db.leads_to_call()[0]
        self.sent = []
        self.built = []

    def _run(self, email):
        def gen(svc, lead):
            self.built.append(lead["name"])
            return "<html>site</html>"

        def deploy(svc, site_id, html, extra_files=None):
            return {"site_id": "s1", "url": "https://preview.example"}

        def send(svc, *, to, subject, body_text, in_reply_to_rfc_id=None):
            self.sent.append((to, subject, body_text))
            return {"rfc_id": "r1"}

        with mock.patch.object(self.core.Services, "generate_site_html", gen), \
                mock.patch.object(self.core.Services, "netlify_deploy", deploy), \
                mock.patch.object(self.core.Services, "email_send", send):
            return self.dash.STATE.agent.said_yes(self.lead["id"], email)

    def test_it_builds_and_sends_the_preview(self):
        r = self._run("joe@rivera.example")
        self.assertTrue(r["ok"])
        self.assertEqual(self.built, ["Rivera Plumbing"])
        self.assertEqual(self.sent[0][0], "joe@rivera.example")
        self.assertEqual(self.dash.STATE.db.get_lead(self.lead["id"])["stage"],
                         self.core.STAGE_PREVIEW_SENT)

    def test_it_records_that_the_address_came_from_the_call(self):
        """Where an address came from is the difference between this app and a
        cold-email app, so it is written down rather than assumed."""
        self._run("joe@rivera.example")
        lead = self.dash.STATE.db.get_lead(self.lead["id"])
        self.assertEqual(lead["email_source"], "given on the phone")

    def test_a_mistyped_address_sends_nothing_at_all(self):
        r = self._run("joe at rivera dot example")
        self.assertFalse(r["ok"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.built, [])
        self.assertEqual(self.dash.STATE.db.get_lead(self.lead["id"])["stage"],
                         self.core.STAGE_FOUND)

    def test_the_preview_that_goes_out_is_watermarked(self):
        """They have not paid yet. What they can see must say so."""
        marked = {}

        def deploy(svc, site_id, html, extra_files=None):
            marked["html"] = html
            return {"site_id": "s1", "url": "https://preview.example"}

        with mock.patch.object(self.core.Services, "generate_site_html",
                               lambda s, l: "<html><body>x</body></html>"), \
                mock.patch.object(self.core.Services, "netlify_deploy", deploy), \
                mock.patch.object(self.core.Services, "email_send",
                                  lambda s, **k: {"rfc_id": "r"}):
            self.dash.STATE.agent.said_yes(self.lead["id"], "a@b.example")
        self.assertNotEqual(marked["html"], "<html><body>x</body></html>")


class NothingGoesOutUnpaidTest(_Base):
    """The rule that was set on day one and has not moved since."""

    def setUp(self):
        super().setUp()
        self.find([place("Rivera Plumbing")])
        lead = self.dash.STATE.db.leads_to_call()[0]
        self.lead_id = lead["id"]
        self.dash.STATE.db.update_lead(
            self.lead_id, email="a@b.example", site_html="<html>clean</html>",
            stripe_session_id="cs_1", netlify_site_id="s1")
        self.dash.STATE.db.claim(self.lead_id, [self.core.STAGE_FOUND],
                                 self.core.STAGE_DEPLOYING_FINAL)

    def test_an_unpaid_session_never_deploys_the_clean_site(self):
        deploys = []
        with mock.patch.object(self.core.Services, "stripe_get_session",
                               lambda s, sid: {"payment_status": "unpaid"}), \
                mock.patch.object(self.core.Services, "netlify_deploy",
                                  lambda *a, **k: deploys.append(1)):
            self.dash.STATE.agent._advance_delivery(self.lead_id)
        self.assertEqual(deploys, [])
        self.assertEqual(self.dash.STATE.db.get_lead(self.lead_id)["stage"],
                         self.core.STAGE_PAYMENT_LINK_SENT)

    def test_stripe_is_asked_again_at_the_moment_of_delivery(self):
        """Not just when the payment landed. Two checks, because this is the
        one step that cannot be taken back."""
        asked = []
        with mock.patch.object(self.core.Services, "stripe_get_session",
                               lambda s, sid: (asked.append(sid),
                                               {"payment_status": "paid"})[1]), \
                mock.patch.object(self.core.Services, "netlify_deploy",
                                  lambda s, sid, html, extra_files=None: {
                                      "site_id": "s1",
                                      "url": "https://live.example"}), \
                mock.patch.object(self.core.Services, "email_send",
                                  lambda s, **k: {"rfc_id": "r"}):
            self.dash.STATE.agent._advance_delivery(self.lead_id)
        self.assertEqual(asked, ["cs_1"])
        self.assertEqual(self.dash.STATE.db.get_lead(self.lead_id)["stage"],
                         self.core.STAGE_DELIVERED)


class NoColdEmailTest(_Base):
    """This app has no way to email a stranger, and that is a feature.

    The previous app's whole front end was cold outreach, and it is the part
    that did not work and carried the legal exposure. It should not be possible
    to bring it back by accident.
    """

    def test_there_is_no_send_outreach_anywhere(self):
        for gone in ("send_outreach", "render_outreach",
                     "research_missing_emails", "_find_email"):
            with self.subTest(gone=gone):
                self.assertFalse(hasattr(self.core.Agent, gone))

    def test_the_background_round_contacts_nobody_new(self):
        """tick() is the only thing that runs unattended. What it does must be
        limited to work that follows a yes."""
        sent = []
        with mock.patch.object(self.core.Services, "email_send",
                               lambda s, **k: sent.append(k)):
            self.dash.STATE.agent.tick()
        self.assertEqual(sent, [])

    def test_a_lead_that_was_never_called_has_no_address_to_leak(self):
        self.find([place("Blank Co")])
        self.assertFalse(self.dash.STATE.db.leads_to_call()[0]["email"])


class CallBacksTest(_Base):
    def setUp(self):
        super().setUp()
        self.find([place("A"), place("B", phone="845-555-0111")])

    def test_not_interested_leaves_the_sheet(self):
        lead = self.dash.STATE.db.leads_to_call()[0]
        self.dash.STATE.agent.mark_called(lead["id"], "not_interested")
        self.assertNotIn(lead["name"],
                         [l["name"] for l in self.dash.STATE.db.leads_to_call()])

    def test_a_call_back_comes_due_and_returns_to_the_sheet(self):
        lead = self.dash.STATE.db.leads_to_call()[0]
        self.dash.STATE.agent.mark_called(lead["id"], "call_back",
                                          call_back_hours=4)
        names = [l["name"] for l in self.dash.STATE.db.leads_to_call()]
        self.assertNotIn(lead["name"], names)          # not yet
        self.dash.STATE.db.update_lead(lead["id"],
                                       call_back_at="2000-01-01T00:00:00+00:00")
        self.assertIn(lead["name"],
                      [l["name"] for l in self.dash.STATE.db.leads_to_call()])

    def test_a_due_call_back_goes_to_the_top(self):
        """Someone who asked you to try again at three should not be behind
        ninety strangers."""
        second = self.dash.STATE.db.leads_to_call()[1]
        self.dash.STATE.agent.mark_called(second["id"], "call_back")
        self.dash.STATE.db.update_lead(second["id"],
                                       call_back_at="2000-01-01T00:00:00+00:00")
        self.assertEqual(self.dash.STATE.db.leads_to_call()[0]["name"],
                         second["name"])

    def test_a_call_is_only_a_note_and_sends_nothing(self):
        lead = self.dash.STATE.db.leads_to_call()[0]
        sent = []
        with mock.patch.object(self.core.Services, "email_send",
                               lambda s, **k: sent.append(k)):
            self.dash.STATE.agent.mark_called(lead["id"], "call_back",
                                              notes="asked me to try Tuesday")
        self.assertEqual(sent, [])
        self.assertIn("Tuesday",
                      self.dash.STATE.db.get_lead(lead["id"])["call_notes"])


class SpendingTest(_Base):
    """Finding businesses must stay inside Google's free allowance on its own.
    The last app's running costs are why it stopped working."""

    def test_the_cap_sits_under_the_free_allowance(self):
        self.assertLess(self.core.GOOGLE_CALL_CAP,
                        self.core.GOOGLE_FREE_CALLS_MONTH)

    def test_it_refuses_to_search_past_the_cap(self):
        self.dash.STATE.db.set_kv(self.core._google_meter_key(),
                                  str(self.core.GOOGLE_CALL_CAP))
        with self.assertRaises(self.core.ServiceError):
            self.dash.STATE.agent._spend_google_call()

    def test_claude_is_never_asked_to_design_before_a_yes(self):
        """A site costs about a dollar. The old app spent on strangers; this one
        cannot, because nothing reaches the build stage without said_yes."""
        designs = []
        with mock.patch.object(self.core.Services, "generate_site_html",
                               lambda s, l: designs.append(l) or "<html></html>"):
            self.find([place("Blank Co")])
            self.dash.STATE.agent.tick()
        self.assertEqual(designs, [])


class TheSheetStocksItselfTest(_Base):
    def test_it_goes_looking_when_the_sheet_runs_low(self):
        with mock.patch.object(self.core.Agent, "find_businesses",
                               return_value={"ok": True, "added": 3}) as f:
            self.dash.STATE.agent.top_up_call_sheet()
        f.assert_called_once()

    def test_it_leaves_a_stocked_sheet_alone(self):
        for i in range(30):
            self.dash.STATE.db.add_lead(
                place_id="p%d" % i, name="P%d" % i, address="a",
                phone="845-555-01%02d" % i, category="Plumber")
        with mock.patch.object(self.core.Agent, "find_businesses") as f:
            r = self.dash.STATE.agent.top_up_call_sheet()
        f.assert_not_called()
        self.assertIn("skipped", r)

    def test_an_area_with_nothing_new_is_not_searched_again_and_again(self):
        """A small town can simply be exhausted. Searching it every minute
        finds the same businesses and still spends the Google calls — the
        silent repeating spend that sank the last app."""
        with mock.patch.object(self.core.Agent, "find_businesses",
                               return_value={"ok": True, "added": 0}) as f:
            self.dash.STATE.agent.top_up_call_sheet()      # searches, finds nothing
            self.dash.STATE.agent.top_up_call_sheet()      # must not search again
            self.dash.STATE.agent.top_up_call_sheet()
        self.assertEqual(f.call_count, 1)

    def test_it_starts_looking_again_once_the_rest_is_over(self):
        with mock.patch.object(self.core.Agent, "find_businesses",
                               return_value={"ok": True, "added": 0}) as f:
            self.dash.STATE.agent.top_up_call_sheet()
            self.dash.STATE.db.set_kv("last_dry_search",
                                      "2000-01-01T00:00:00+00:00")
            self.dash.STATE.agent.top_up_call_sheet()
        self.assertEqual(f.call_count, 2)

    def test_a_search_that_did_find_something_does_not_trigger_the_rest(self):
        with mock.patch.object(self.core.Agent, "find_businesses",
                               return_value={"ok": True, "added": 4}) as f:
            self.dash.STATE.agent.top_up_call_sheet()
            self.dash.STATE.agent.top_up_call_sheet()
        self.assertEqual(f.call_count, 2)

    def test_with_no_area_set_it_does_nothing_rather_than_guessing(self):
        cfg = self.core.load_config()
        cfg["territory_base"] = ""
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        self.assertIn("skipped", self.dash.STATE.agent.top_up_call_sheet())


class ThePageTest(_Base):
    def setUp(self):
        super().setUp()
        self.find([place("Rivera Plumbing")])
        self.client = self.dash.app.test_client()

    def get(self, path="/"):
        return self.client.get(
            path, environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()

    def test_the_call_sheet_leads_with_a_number_you_can_tap(self):
        html = self.get()
        self.assertIn('href="tel:845-555-0100"', html)
        self.assertIn("Rivera Plumbing", html)

    def test_it_puts_the_words_in_front_of_you(self):
        self.assertIn("best email to send it to", self.get())

    def test_an_empty_sheet_says_what_to_do(self):
        self.dash.STATE.db._conn().execute("DELETE FROM leads")
        self.dash.STATE.db._conn().commit()
        self.assertIn("Nobody to call yet", self.get())

    def test_no_template_hides_itself_behind_a_jinja_comment(self):
        """`{#` opens a comment in Jinja. It has silently blanked a page twice
        in this codebase's history, so no template may contain one."""
        for name, tpl in self.dash.app.jinja_env.loader.mapping.items():
            with self.subTest(template=name):
                self.assertNotIn("{#", tpl)

    def test_every_page_renders(self):
        for path in ("/", "/pipeline", "/setup", "/activity"):
            with self.subTest(path=path):
                r = self.client.get(path,
                                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
                self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
