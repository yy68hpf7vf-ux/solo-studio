"""Self-update tests.

The updater downloads code from the internet and runs it, so the property that
matters most is: a bad download must never replace a working install.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOOD_PY = "# a believable module\n" + "x = 1\n" * 400


class FakeResp:
    def __init__(self, text="", status=200, payload=None):
        self.text, self.status_code, self._payload = text, status, payload or {}

    def json(self):
        return self._payload


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-upd-")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        import solo_studio_agent as core
        self.core = core
        self.dir = core.updates_dir()

    def tearDown(self):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_good(self, sha="aaaaaaa"):
        for name in ("solo_studio_agent.py", "dashboard_app.py"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(GOOD_PY)
        with open(os.path.join(self.dir, "installed.json"), "w") as f:
            json.dump({"sha": sha, "applied_at": "now"}, f)

    def _snapshot(self):
        return {f: open(os.path.join(self.dir, f)).read()
                for f in os.listdir(self.dir)}

    # -- checking ---------------------------------------------------------

    def test_check_reports_an_update_when_sha_differs(self):
        self._install_good("old-sha")
        payload = {"sha": "new-sha-1234567",
                   "commit": {"message": "Fix a thing\n\nbody",
                              "committer": {"date": "2026-09-06T00:00:00Z"}}}
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp(payload=payload)):
            info = self.core.check_for_update()
        self.assertTrue(info["ok"])
        self.assertTrue(info["available"])
        self.assertEqual(info["message"], "Fix a thing")

    def test_check_reports_up_to_date(self):
        self._install_good("same-sha")
        payload = {"sha": "same-sha", "commit": {"message": "m",
                   "committer": {"date": "2026-09-06T00:00:00Z"}}}
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp(payload=payload)):
            self.assertFalse(self.core.check_for_update()["available"])

    def test_check_handles_github_being_unreachable(self):
        import requests
        with mock.patch("solo_studio_agent.requests.get",
                        side_effect=requests.RequestException("no network")):
            info = self.core.check_for_update()
        self.assertFalse(info["ok"])
        self.assertIn("Couldn't reach GitHub", info["error"])

    # -- applying ---------------------------------------------------------

    def test_successful_update_writes_files_and_records_version(self):
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp(GOOD_PY)):
            r = self.core.apply_update(sha="abc1234")
        self.assertTrue(r["ok"])
        self.assertEqual(self.core.installed_version()["sha"], "abc1234")
        for name in self.core.UPDATE_FILES:
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)), name)

    def test_html_error_page_is_refused_and_changes_nothing(self):
        self._install_good()
        before = self._snapshot()
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp("<html>404 Not Found</html>")):
            r = self.core.apply_update(sha="bad")
        self.assertFalse(r["ok"])
        self.assertIn("isn't valid Python", r["error"])
        self.assertEqual(self._snapshot(), before)

    def test_broken_python_is_refused_and_changes_nothing(self):
        self._install_good()
        before = self._snapshot()
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp("def broken(:\n" + "pad\n" * 400)):
            r = self.core.apply_update(sha="bad")
        self.assertFalse(r["ok"])
        self.assertEqual(self._snapshot(), before)

    def test_truncated_download_is_refused(self):
        self._install_good()
        before = self._snapshot()
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp("x = 1\n")):
            r = self.core.apply_update(sha="bad")
        self.assertFalse(r["ok"])
        self.assertIn("truncated", r["error"])
        self.assertEqual(self._snapshot(), before)

    def test_http_failure_is_refused(self):
        self._install_good()
        before = self._snapshot()
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp("nope", status=503)):
            r = self.core.apply_update(sha="bad")
        self.assertFalse(r["ok"])
        self.assertEqual(self._snapshot(), before)

    def test_a_later_file_failing_leaves_earlier_ones_alone(self):
        """Nothing is written until every file has downloaded and compiled."""
        self._install_good()
        before = self._snapshot()
        calls = {"n": 0}

        def flaky(url, **kw):
            calls["n"] += 1
            return FakeResp(GOOD_PY) if calls["n"] == 1 else FakeResp("bad(:\n" * 400)

        with mock.patch("solo_studio_agent.requests.get", side_effect=flaky):
            r = self.core.apply_update(sha="bad")
        self.assertFalse(r["ok"])
        self.assertEqual(self._snapshot(), before)

    def test_update_never_touches_config_or_database(self):
        """Leads and API keys live outside the code folder and must survive."""
        cfg = self.core.load_config()
        cfg["stripe_secret_key"] = "sk_test_keepme"
        self.core.save_config(cfg)
        db = self.core.Database()
        db.add_lead(place_id="p1", name="Keep Me", address=None, phone=None,
                    category=None, email="keep@example.com")
        with mock.patch("solo_studio_agent.requests.get",
                        return_value=FakeResp(GOOD_PY)):
            self.assertTrue(self.core.apply_update(sha="abc")["ok"])
        self.assertEqual(self.core.load_config()["stripe_secret_key"],
                         "sk_test_keepme")
        self.assertEqual(len(self.core.Database().all_leads()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
