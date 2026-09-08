"""Plain-English service errors.

A raw API failure is a paragraph of JSON that gets cut off mid-word in a flash
message. What the user needs is the one sentence that says what to do.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import solo_studio_agent as core   # noqa: E402


ANTHROPIC_NO_CREDIT = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low to "
    "access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits.'}}")


class ExplainTest(unittest.TestCase):

    def test_out_of_credit_says_where_to_add_it(self):
        """The one that actually happened: 200 characters of JSON, truncated
        at 'purcha', with the answer past the cut."""
        plain = core.explain(Exception(ANTHROPIC_NO_CREDIT))
        self.assertIn("console.anthropic.com", plain)
        self.assertIn("Billing", plain)
        self.assertNotIn("{", plain)
        self.assertNotIn("invalid_request_error", plain)

    def test_out_of_credit_does_not_blame_the_key(self):
        """Sending the user off to replace a working key wastes their evening."""
        self.assertIn("key is fine", core.explain(Exception(ANTHROPIC_NO_CREDIT)))

    def test_a_rejected_key_does_say_to_replace_it(self):
        plain = core.explain(Exception(
            "Error code: 401 - {'error': {'type': 'authentication_error', "
            "'message': 'invalid x-api-key'}}"))
        self.assertIn("Setup", plain)

    def test_google_refusing_the_search(self):
        plain = core.explain(Exception(
            "Google Places error 403: {'error': {'status': 'REQUEST_DENIED'}}"))
        self.assertIn("Places API", plain)

    def test_no_internet(self):
        plain = core.explain(Exception(
            "HTTPSConnectionPool(host='api.anthropic.com', port=443): Max "
            "retries exceeded with url: /v1/messages"))
        self.assertIn("connection", plain.lower())

    def test_anything_unrecognised_comes_through_as_it_is(self):
        """A raw message still beats a vague one."""
        self.assertEqual(core.explain(Exception("a thing nobody predicted")),
                         "a thing nobody predicted")

    def test_unrecognised_messages_are_trimmed_not_dropped(self):
        self.assertEqual(len(core.explain(Exception("x" * 900), 120)), 120)

    def test_a_translated_message_is_never_truncated_mid_word(self):
        """The whole point: the sentence has to survive the trim."""
        for _, plain in core.PLAIN_ERRORS:
            with self.subTest(plain=plain[:40]):
                self.assertTrue(plain.rstrip().endswith("."))
                self.assertLessEqual(len(plain), 200)


class WiredUpTest(unittest.TestCase):
    """Every place the user reads an error has to go through it."""

    def test_the_pages_that_show_errors_translate_them(self):
        import inspect
        import dashboard_app as dash
        for fn in ("build_searches", "setup_test", "ask_send"):
            src = inspect.getsource(getattr(dash, fn))
            with self.subTest(route=fn):
                self.assertIn("explain(", src)
                self.assertNotIn("str(e)[:", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
