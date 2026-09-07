"""The line of the day: steady all day, new tomorrow, and never misattributed."""

import os
import shutil
import sys
import tempfile
import html
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DailyLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-daily-")
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

    def test_the_same_line_all_day(self):
        day = date(2026, 9, 7)
        first = self.core.line_for_today(day)
        for _ in range(5):
            self.assertEqual(self.core.line_for_today(day), first)

    def test_a_different_line_tomorrow(self):
        day = date(2026, 9, 7)
        self.assertNotEqual(self.core.line_for_today(day),
                            self.core.line_for_today(day + timedelta(days=1)))

    def test_the_whole_list_comes_round_before_repeating(self):
        n = len(self.core.DAILY_LINES)
        start = date(2026, 1, 1)
        seen = [self.core.line_for_today(start + timedelta(days=i))["text"]
                for i in range(n)]
        self.assertEqual(len(set(seen)), n, "a line repeated within one cycle")
        # and it wraps rather than running off the end
        self.assertEqual(self.core.line_for_today(start + timedelta(days=n)),
                         self.core.line_for_today(start))

    def test_it_holds_up_years_out(self):
        for years in (1, 5, 20):
            day = date.today() + timedelta(days=365 * years)
            line = self.core.line_for_today(day)
            self.assertIn(line["text"], [t for t, _s in self.core.DAILY_LINES])

    def test_every_line_is_usable(self):
        for text, source in self.core.DAILY_LINES:
            with self.subTest(line=text[:40]):
                self.assertTrue(text.strip())
                self.assertLess(len(text), 160, "too long to read at a glance")
                self.assertEqual(text, text.strip())
                # attribution is optional, but must be a real name when present
                self.assertNotIn("Anonymous", source)
                self.assertNotIn("Unknown", source)

    def test_it_shows_up_on_the_dashboard(self):
        # the page escapes apostrophes, so compare against unescaped text
        page = html.unescape(self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode())
        self.assertIn(self.core.line_for_today()["text"], page)

    def test_it_works_before_any_keys_are_added(self):
        """No API involved — it must read fine on a brand-new install."""
        cfg = self.core.load_config()
        for k in ("anthropic_api_key", "inkbox_api_key"):
            cfg[k] = ""
        self.core.save_config(cfg)
        self.dash.STATE.reload()
        r = self.client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.core.line_for_today()["text"],
                      html.unescape(r.data.decode()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
