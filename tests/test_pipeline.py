"""State-machine stress tests for the Solo Studio pipeline.

These run against fake services (no network) and exist to prove the hard
guarantees: no final site without a Stripe-verified payment, no double
charges, no double previews, no lost paid leads.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeServices:
    """Duck-types solo_studio_agent.Services. Keyword-based reply classifier."""

    def __init__(self):
        self.sent_emails = []          # dicts: to, subject, body_text, in_reply_to
        self.inbound_queue = []        # message dicts, as email_inbound_since returns
        self.bodies = {}               # message_uuid -> body text
        self.deploys = []              # (site_id, html)
        self.sites = 0
        self.sessions = {}             # id -> {status, payment_status, url}
        self.generate_calls = 0
        self.checkout_calls = 0
        self.fail_once = set()         # method names that should raise once
        self._email_counter = 0

    def _maybe_fail(self, name):
        if name in self.fail_once:
            self.fail_once.discard(name)
            raise RuntimeError(f"injected failure in {name}")

    # -- places ----------------------------------------------------------
    def places_search_no_website(self, query, max_results=60):
        return [{"place_id": "p1", "name": "Joe's Plumbing",
                 "address": "1 Main St", "phone": "555-0100",
                 "category": "Plumber"}]

    # -- email -----------------------------------------------------------
    def email_send(self, *, to, subject, body_text, in_reply_to_rfc_id=None):
        self._maybe_fail("email_send")
        self._email_counter += 1
        self.sent_emails.append({"to": to, "subject": subject,
                                 "body_text": body_text,
                                 "in_reply_to": in_reply_to_rfc_id})
        return {"thread_id": f"thread-{to}", "rfc_id": f"<out-{self._email_counter}@x>",
                "message_uuid": f"out-uuid-{self._email_counter}"}

    def email_inbound_since(self, since_iso):
        return list(self.inbound_queue)

    def email_fetch_body(self, message_uuid):
        return self.bodies.get(message_uuid, "")

    # -- claude ----------------------------------------------------------
    def generate_site_html(self, lead):
        self._maybe_fail("generate_site_html")
        self.generate_calls += 1
        return ("<!doctype html><html><head><title>site</title></head>"
                f"<body><h1>{lead['name']}</h1></body></html>")

    def classify_reply(self, lead, stage, body):
        b = body.lower()
        if "unsubscribe" in b:
            return "unsubscribe"
        if "no thanks" in b:
            return "declined"
        if "yes" in b or "love" in b:
            return "interested"
        return "unclear"

    # -- netlify ---------------------------------------------------------
    def netlify_deploy(self, site_id, html, extra_files=None):
        self._maybe_fail("netlify_deploy")
        if site_id is None:
            self.sites += 1
            site_id = f"site-{self.sites}"
        self.deploys.append((site_id, html))
        return {"site_id": site_id, "url": f"https://{site_id}.netlify.app"}

    # -- stripe ----------------------------------------------------------
    def stripe_create_checkout(self, lead, preview_url):
        self._maybe_fail("stripe_create_checkout")
        self.checkout_calls += 1
        sid = f"cs_{self.checkout_calls}"
        self.sessions[sid] = {"status": "open", "payment_status": "unpaid",
                              "url": f"https://checkout.stripe.com/{sid}"}
        return {"session_id": sid, "url": self.sessions[sid]["url"]}

    def stripe_get_session(self, session_id):
        return dict(self.sessions[session_id])

    # -- test helpers ----------------------------------------------------
    def pay(self, session_id):
        self.sessions[session_id].update(status="complete", payment_status="paid")

    def expire(self, session_id):
        self.sessions[session_id].update(status="expired")

    def add_reply(self, uuid_, thread_id, from_addr, body, created="2026-01-01T00:00:00"):
        self.inbound_queue.append({
            "message_uuid": uuid_, "thread_id": thread_id,
            "rfc_id": f"<{uuid_}@lead>", "from_address": from_addr,
            "subject": "Re: your email", "snippet": body[:60],
            "created_at": created})
        self.bodies[uuid_] = body


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-test-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        global core
        import solo_studio_agent as core_mod
        core = core_mod
        self.db = core.Database(os.path.join(self.tmp, "test.db"))
        self.svc = FakeServices()
        self.config = dict(core.DEFAULT_CONFIG)
        self.config.update(your_name="Alex", mailing_address="9 Studio Rd",
                           site_price_usd=500)
        self.agent = core.Agent(self.db, self.svc, self.config)

    def tearDown(self):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def make_contacted_lead(self):
        self.agent.find_leads("plumbers")
        lead = self.db.all_leads()[0]
        self.agent.set_email(lead["id"], "joe@example.com")
        r = self.agent.send_outreach(lead["id"])
        self.assertTrue(r["ok"], r)
        return self.db.get_lead(lead["id"])

    def stage(self, lead_id):
        return self.db.get_lead(lead_id)["stage"]

    # -- tests -------------------------------------------------------------

    def test_happy_path_end_to_end(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.assertEqual(self.stage(lid), core.STAGE_CONTACTED)
        self.assertEqual(len(self.svc.sent_emails), 1)
        self.assertIn("9 Studio Rd", self.svc.sent_emails[0]["body_text"])

        # Reply 1: interested -> watermarked preview deployed + emailed.
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com",
                           "Yes, I'd love a preview!")
        self.agent.process_replies()
        self.assertEqual(self.stage(lid), core.STAGE_PREVIEW_SENT)
        self.assertEqual(self.svc.generate_calls, 1)
        self.assertEqual(len(self.svc.deploys), 1)
        site_id, preview_html = self.svc.deploys[0]
        self.assertIn("ss-preview-banner", preview_html)   # watermarked
        self.assertIn("netlify.app", self.svc.sent_emails[1]["body_text"])
        # Preview email threads onto the lead's reply.
        self.assertEqual(self.svc.sent_emails[1]["in_reply_to"], "<m1@lead>")

        # Reply 2: they like it -> ONE checkout session + link emailed.
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com",
                           "yes let's do it")
        self.agent.process_replies()
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)
        self.assertEqual(self.svc.checkout_calls, 1)
        self.assertIn("checkout.stripe.com", self.svc.sent_emails[2]["body_text"])

        # Unpaid poll: nothing moves.
        self.agent.poll_payments()
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)
        self.assertEqual(len(self.svc.deploys), 1)

        # Payment clears -> clean deploy to the SAME site + delivery email.
        self.svc.pay("cs_1")
        self.agent.poll_payments()
        self.assertEqual(self.stage(lid), core.STAGE_DELIVERED)
        self.assertEqual(len(self.svc.deploys), 2)
        final_site_id, final_html = self.svc.deploys[1]
        self.assertEqual(final_site_id, site_id)           # same URL upgrades
        self.assertNotIn("ss-preview-banner", final_html)  # watermark gone
        self.assertIn("LIVE", self.svc.sent_emails[3]["body_text"])
        lead = self.db.get_lead(lid)
        self.assertIsNotNone(lead["paid_at"])
        self.assertIsNotNone(lead["delivered_at"])

    def test_duplicate_reply_processing_is_idempotent(self):
        lead = self.make_contacted_lead()
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes please")
        self.agent.process_replies()
        self.agent.process_replies()   # same inbound message again
        self.agent.process_replies()
        self.assertEqual(self.svc.generate_calls, 1)
        self.assertEqual(len(self.svc.deploys), 1)
        # 1 outreach + 1 preview email, nothing more.
        self.assertEqual(len(self.svc.sent_emails), 2)

    def test_reply_after_payment_link_never_creates_second_session(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes!")
        self.agent.process_replies()
        self.assertEqual(self.svc.checkout_calls, 1)
        # Third and fourth enthusiastic replies: same link re-sent, no new session.
        self.svc.add_reply("m3", lead["thread_id"], "joe@example.com", "yes yes")
        self.svc.add_reply("m4", lead["thread_id"], "joe@example.com", "I love it")
        self.agent.process_replies()
        self.assertEqual(self.svc.checkout_calls, 1)
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)
        resends = [e for e in self.svc.sent_emails
                   if "buried" in e["body_text"]]
        self.assertEqual(len(resends), 2)
        for e in resends:
            self.assertIn("cs_1", e["body_text"])

    def test_no_delivery_without_payment(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        # Hammer every advancing entrypoint while unpaid.
        for _ in range(3):
            self.agent.poll_payments()
            self.agent.tick_transients()
        r = self.agent.manual_advance(lid)
        self.assertFalse(r["ok"])  # no manual bypass of the payment gate
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)
        self.assertEqual(len(self.svc.deploys), 1)  # preview only, never final

    def test_double_poll_after_payment_delivers_once(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.pay("cs_1")
        self.agent.poll_payments()
        self.agent.poll_payments()
        self.agent.tick_transients()
        self.assertEqual(self.stage(lid), core.STAGE_DELIVERED)
        self.assertEqual(len(self.svc.deploys), 2)  # 1 preview + 1 final
        delivery_emails = [e for e in self.svc.sent_emails
                           if "live" in e["subject"].lower()]
        self.assertEqual(len(delivery_emails), 1)

    def test_delivery_failure_keeps_paid_lead_and_retries(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.pay("cs_1")
        self.svc.fail_once.add("netlify_deploy")
        self.agent.poll_payments()
        # Deploy failed AFTER payment: lead must be parked at 'paid', not lost.
        self.assertEqual(self.stage(lid), core.STAGE_PAID)
        self.assertIsNotNone(self.db.get_lead(lid)["paid_at"])
        # Next poll retries and succeeds; still exactly one charge.
        self.agent.poll_payments()
        self.assertEqual(self.stage(lid), core.STAGE_DELIVERED)
        self.assertEqual(self.svc.checkout_calls, 1)

    def test_unmatched_reply_goes_to_attention(self):
        self.make_contacted_lead()
        self.svc.add_reply("mX", "some-other-thread", "stranger@example.com",
                           "who is this?")
        self.agent.process_replies()
        kinds = [e["kind"] for e in self.db.recent_events(20)]
        self.assertIn("unmatched_reply", kinds)
        self.assertEqual(len(self.svc.sent_emails), 1)  # nothing auto-sent

    def test_unclear_reply_takes_no_action(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com",
                           "How much does hosting cost after the first year?")
        self.agent.process_replies()
        self.assertEqual(self.stage(lid), core.STAGE_CONTACTED)  # unchanged
        self.assertEqual(len(self.svc.sent_emails), 1)           # nothing sent
        self.assertTrue(any(e["needs_attention"] for e in self.db.recent_events(10)))

    def test_unsubscribe_flags_do_not_contact(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com",
                           "unsubscribe me please")
        self.agent.process_replies()
        lead = self.db.get_lead(lid)
        self.assertEqual(lead["stage"], core.STAGE_NOT_INTERESTED)
        self.assertEqual(lead["do_not_contact"], 1)
        # And they can never be cold-emailed again.
        self.db.update_lead(lid, stage=core.STAGE_FOUND)  # even if stage reset
        r = self.agent.send_outreach(lid)
        self.assertFalse(r["ok"])

    def test_outreach_failure_rolls_back_and_can_retry(self):
        self.agent.find_leads("plumbers")
        lid = self.db.all_leads()[0]["id"]
        self.agent.set_email(lid, "joe@example.com")
        self.svc.fail_once.add("email_send")
        r = self.agent.send_outreach(lid)
        self.assertFalse(r["ok"])
        self.assertEqual(self.stage(lid), core.STAGE_FOUND)  # rolled back
        r = self.agent.send_outreach(lid)
        self.assertTrue(r["ok"])
        self.assertEqual(self.stage(lid), core.STAGE_CONTACTED)
        self.assertEqual(len(self.svc.sent_emails), 1)

    def test_double_outreach_click_sends_once(self):
        self.agent.find_leads("plumbers")
        lid = self.db.all_leads()[0]["id"]
        self.agent.set_email(lid, "joe@example.com")
        self.assertTrue(self.agent.send_outreach(lid)["ok"])
        self.assertFalse(self.agent.send_outreach(lid)["ok"])  # claim blocks it
        self.assertEqual(len(self.svc.sent_emails), 1)

    def test_crash_resume_does_not_regenerate_or_redeploy(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        # Preview build crashes at the email step (site generated + deployed).
        self.svc.fail_once.add("email_send")
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.assertEqual(self.stage(lid), core.STAGE_BUILDING_PREVIEW)
        self.assertEqual(self.svc.generate_calls, 1)
        self.assertEqual(len(self.svc.deploys), 1)
        # Background tick resumes: no second generation, no second deploy.
        self.agent.tick_transients()
        self.assertEqual(self.stage(lid), core.STAGE_PREVIEW_SENT)
        self.assertEqual(self.svc.generate_calls, 1)
        self.assertEqual(len(self.svc.deploys), 1)

    def test_repeated_failures_park_in_error_and_retry_works(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        for _ in range(core.MAX_ATTEMPTS):
            self.svc.fail_once.add("generate_site_html")
            if self.stage(lid) == core.STAGE_CONTACTED:
                self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
                self.agent.process_replies()
            else:
                self.agent.tick_transients()
        self.assertEqual(self.stage(lid), core.STAGE_ERROR)
        r = self.agent.retry_from_error(lid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.stage(lid), core.STAGE_PREVIEW_SENT)

    def test_expired_session_replacement_increments_generation(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        # While the session is open, a new link is refused.
        r = self.agent.new_payment_link(lid)
        self.assertFalse(r["ok"])
        self.assertEqual(self.svc.checkout_calls, 1)
        # After expiry it is allowed, and the generation counter moves.
        self.svc.expire("cs_1")
        r = self.agent.new_payment_link(lid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.svc.checkout_calls, 2)
        lead = self.db.get_lead(lid)
        self.assertEqual(lead["checkout_generation"], 1)
        self.assertEqual(lead["stripe_session_id"], "cs_2")
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)

    def test_paid_lead_cannot_get_new_payment_link(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.pay("cs_1")
        r = self.agent.new_payment_link(lid)
        self.assertFalse(r["ok"])
        self.assertIn("already paid", r["error"])
        self.assertEqual(self.svc.checkout_calls, 1)

    def test_reply_matched_by_email_when_thread_unknown(self):
        lead = self.make_contacted_lead()
        lid = lead["id"]
        # Lead replies from a fresh thread (e.g. wrote a new email instead).
        self.svc.add_reply("m1", None, "joe@example.com", "yes do it")
        self.agent.process_replies()
        self.assertEqual(self.stage(lid), core.STAGE_PREVIEW_SENT)

    def test_delivery_blocked_if_stripe_flips_unpaid(self):
        """Paranoia test: if Stripe stops reporting 'paid' between the poll and
        the deploy (refund/webhook weirdness), the final never ships."""
        lead = self.make_contacted_lead()
        lid = lead["id"]
        self.svc.add_reply("m1", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.add_reply("m2", lead["thread_id"], "joe@example.com", "yes")
        self.agent.process_replies()
        self.svc.pay("cs_1")

        original_get = self.svc.stripe_get_session
        calls = {"n": 0}

        def flappy(session_id):
            calls["n"] += 1
            s = original_get(session_id)
            if calls["n"] >= 2:   # first check (poll) paid, re-check unpaid
                s["payment_status"] = "unpaid"
            return s

        self.svc.stripe_get_session = flappy
        self.agent.poll_payments()
        self.assertEqual(self.stage(lid), core.STAGE_PAYMENT_LINK_SENT)
        self.assertEqual(len(self.svc.deploys), 1)  # preview only


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-conc-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core_mod
        self.core = core_mod
        self.db = core_mod.Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parallel_claims_only_one_wins(self):
        import threading
        core = self.core
        lid = self.db.add_lead(place_id="p1", name="X", address=None,
                               phone=None, category=None)
        self.db.update_lead(lid, stage=core.STAGE_PAYMENT_LINK_SENT)
        wins = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            if self.db.claim(lid, [core.STAGE_PAYMENT_LINK_SENT], core.STAGE_PAID):
                wins.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(wins), 1)

    def test_parallel_message_dedupe_only_one_wins(self):
        import threading
        wins = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            if self.db.mark_message_processed("msg-1", None):
                wins.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(wins), 1)
