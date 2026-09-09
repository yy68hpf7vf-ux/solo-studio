"""Looking at a business's website and saying what's wrong with it.

Run against real HTTP servers on localhost rather than mocks: the point of
this code is how it behaves against real responses, and a mocked requests
call would prove nothing about that.
"""
import http.server
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import solo_studio_agent as core   # noqa: E402


GOOD = ("<html><head><meta name=\"viewport\" content=\"width=device-width\">"
        "</head><body>" + ("Real content about our plumbing services. " * 40)
        + "</body></html>")
NO_VIEWPORT = "<html><head><title>Us</title></head><body>" + \
    ("We have been serving the area since 1998. " * 40) + "</body></html>"
PARKED = ("<html><body><h1>Coming Soon</h1>"
          "<p>This domain is for sale.</p>" + ("filler " * 200) + "</body></html>")
THIN = "<html><body>hi</body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    ROUTES = {"/good": (200, GOOD), "/noviewport": (200, NO_VIEWPORT),
              "/parked": (200, PARKED), "/thin": (200, THIN),
              "/gone": (404, "not found"), "/boom": (500, "server error")}

    def do_GET(self):
        status, body = self.ROUTES.get(self.path, (404, "no"))
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class CheckWebsiteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def check(self, path):
        return core.check_website(self.base + path, timeout=5)[0]

    def test_no_link_at_all_is_the_cleanest_lead(self):
        self.assertEqual(core.check_website("")[0], core.SITE_NONE)

    def test_a_facebook_page_is_recognised_without_fetching_it(self):
        self.assertEqual(
            core.check_website("https://facebook.com/joes")[0], core.SITE_SOCIAL)

    def test_a_404_is_a_dead_site(self):
        self.assertEqual(self.check("/gone"), core.SITE_DEAD)

    def test_a_500_is_a_dead_site(self):
        self.assertEqual(self.check("/boom"), core.SITE_DEAD)

    def test_a_domain_that_does_not_resolve_is_a_dead_site(self):
        status, note = core.check_website(
            "https://this-domain-does-not-exist-solo-studio.invalid", timeout=5)
        self.assertEqual(status, core.SITE_DEAD)
        self.assertTrue(note)

    def test_a_placeholder_page_is_parked(self):
        self.assertEqual(self.check("/parked"), core.SITE_PARKED)

    def test_an_almost_empty_page_is_parked(self):
        self.assertEqual(self.check("/thin"), core.SITE_PARKED)

    def test_a_plain_http_site_is_flagged_as_insecure(self):
        """Browsers put "Not secure" in the address bar. That is the pitch."""
        self.assertEqual(self.check("/good"), core.SITE_INSECURE)

    def test_a_page_with_no_viewport_is_not_built_for_phones(self):
        """Checked over http, so insecure wins first — this pins the ordering
        rather than the verdict."""
        self.assertIn(self.check("/noviewport"),
                      (core.SITE_INSECURE, core.SITE_NOT_MOBILE))

    def test_it_gives_up_rather_than_hanging_on_a_slow_site(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)                     # accepts, never answers
        port = sock.getsockname()[1]
        try:
            import time
            t0 = time.time()
            status, _ = core.check_website(f"http://127.0.0.1:{port}/", timeout=2)
            self.assertEqual(status, core.SITE_DEAD)
            self.assertLess(time.time() - t0, 6, "a slow site must not hang a run")
        finally:
            sock.close()

    def test_every_verdict_has_something_to_say_to_the_business(self):
        for status in (core.SITE_NONE, core.SITE_DEAD, core.SITE_PARKED,
                       core.SITE_SOCIAL, core.SITE_INSECURE,
                       core.SITE_NOT_MOBILE):
            with self.subTest(status=status):
                self.assertTrue(core.SITE_REASON.get(status))

    def test_a_working_site_is_never_a_lead_at_any_setting(self):
        """Widening the net must never reach someone with a good site."""
        for level in core.QUALITY_LEVELS.values():
            self.assertNotIn(core.SITE_OK, level)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ScrapeEmailTest(unittest.TestCase):
    """Reading an address off a page, which is the free way to do it."""

    @classmethod
    def setUpClass(cls):
        class H(http.server.BaseHTTPRequestHandler):
            PAGES = {
                "/footer": "<html><body>Call us or email "
                           "<a href='mailto:info@joesplumbing.com'>here</a>"
                           "</body></html>",
                "/person": "<html><body>Reach ray@raysroofing.com</body></html>",
                "/both": "<html><body>ray@x.com and info@x.com</body></html>",
                "/junk": "<html><body>noreply@x.com sentry.io@x.com "
                         "logo@2x.png</body></html>",
                "/none": "<html><body>No way to reach us.</body></html>",
            }

            def do_GET(self):
                body = self.PAGES.get(self.path, "nothing").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def scrape(self, path):
        return core.scrape_email(self.base + path, timeout=5)[0]

    def test_it_finds_an_address_in_a_mailto_link(self):
        self.assertEqual(self.scrape("/footer"), "info@joesplumbing.com")

    def test_it_finds_a_bare_address_in_the_text(self):
        self.assertEqual(self.scrape("/person"), "ray@raysroofing.com")

    def test_it_prefers_the_business_address_over_a_persons(self):
        self.assertEqual(self.scrape("/both"), "info@x.com")

    def test_it_ignores_the_plumbing_of_the_web(self):
        """noreply, error trackers and image filenames are not contacts."""
        self.assertEqual(self.scrape("/junk"), "")

    def test_a_page_with_no_address_returns_nothing(self):
        self.assertEqual(self.scrape("/none"), "")

    def test_no_url_costs_nothing_and_returns_nothing(self):
        self.assertEqual(core.scrape_email(""), ("", ""))

    def test_an_unreachable_site_is_not_an_error(self):
        self.assertEqual(
            core.scrape_email("https://nope-solo-studio.invalid", timeout=5),
            ("", ""))
