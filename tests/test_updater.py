"""Self-update tests.

The updater downloads code from the internet and runs it, so the property that
matters most is: a bad download must never replace a working install.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
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


class RestartRouteTest(unittest.TestCase):
    """How the Restart button decides to come back.

    Guessing whether a launcher was there went wrong in both directions: real
    users were told to quit and reopen when a launcher was running them, and a
    wrong guess the other way exits into nothing and the app disappears. Only
    the launcher's own signal counts now; everything else relaunches itself,
    which is safe whether or not a launcher exists.
    """

    def setUp(self):
        import dashboard_app as dash
        self.dash = dash
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-restart-")
        self.old_home = os.environ.get("SOLO_STUDIO_HOME")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.was = dash.LAUNCHER_RERUNS_US

    def tearDown(self):
        self.dash.LAUNCHER_RERUNS_US = self.was
        if self.old_home is None:
            os.environ.pop("SOLO_STUDIO_HOME", None)
        else:
            os.environ["SOLO_STUDIO_HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restart(self):
        self.dash.app.config["TESTING"] = True
        with self.dash.app.test_client() as client:
            before = set(threading.enumerate())
            with mock.patch.object(self.dash.time, "sleep"):
                r = client.post("/action/restart",
                                environ_base={"REMOTE_ADDR": "127.0.0.1"})
            for t in set(threading.enumerate()) - before:
                t.join(5)          # the goodbye runs in its own thread
            return r

    def test_a_launcher_that_announced_itself_gets_the_restart_code(self):
        self.dash.LAUNCHER_RERUNS_US = True
        with mock.patch.object(self.dash.os, "_exit") as ex, \
                mock.patch.object(self.dash, "_relaunch_self") as relaunch:
            self._restart()
        ex.assert_called_once_with(self.dash.core.RESTART_EXIT_CODE)
        relaunch.assert_not_called()

    def test_everything_else_relaunches_instead_of_exiting(self):
        self.dash.LAUNCHER_RERUNS_US = False
        with mock.patch.object(self.dash.os, "_exit") as ex, \
                mock.patch.object(self.dash, "_relaunch_self") as relaunch:
            self._restart()
        relaunch.assert_called_once()
        ex.assert_not_called()

    def test_only_the_launchers_own_signal_counts(self):
        """Not the directory the code happens to sit in — that is exactly the
        guess that made the app vanish."""
        import inspect
        src = inspect.getsource(self.dash)
        head = src[:src.index("CLOUD_PASSWORD =")]
        self.assertIn("SOLO_STUDIO_LAUNCHER", head)
        self.assertNotIn("Contents", head)


class RelaunchTargetTest(unittest.TestCase):
    """Which copy of the code a self-relaunch comes back on.

    Same rules the launcher uses, because quitting into code that does not
    start would leave the user with no app at all.
    """

    GOOD = "x = 1\n"
    BROKEN = "def (:\n"

    def setUp(self):
        import dashboard_app as dash
        import solo_studio_agent as core
        self.dash, self.core = dash, core
        self.tmp = tempfile.mkdtemp(prefix="solo-studio-relaunch-")
        self.old_home = os.environ.get("SOLO_STUDIO_HOME")
        os.environ["SOLO_STUDIO_HOME"] = self.tmp
        self.updated = self.core.updates_dir()
        self.mine = os.path.join(self.tmp, "running")
        os.makedirs(self.mine, exist_ok=True)
        self._fill(self.mine, self.GOOD)
        self.real_file = dash.__file__
        dash.__file__ = os.path.join(self.mine, "dashboard_app.py")

    def tearDown(self):
        self.dash.__file__ = self.real_file
        if self.old_home is None:
            os.environ.pop("SOLO_STUDIO_HOME", None)
        else:
            os.environ["SOLO_STUDIO_HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, directory, body):
        for name in ("dashboard_app.py", "solo_studio_agent.py"):
            with open(os.path.join(directory, name), "w",
                      encoding="utf-8") as f:
                f.write(body)

    def test_no_download_means_we_come_back_as_ourselves(self):
        self.assertEqual(self.dash._code_to_run(), self.mine)

    def test_a_good_download_is_what_we_come_back_on(self):
        self._fill(self.updated, self.GOOD)
        self.assertEqual(self.dash._code_to_run(), self.updated)

    def test_a_broken_download_is_refused(self):
        self._fill(self.updated, self.BROKEN)
        self.assertEqual(self.dash._code_to_run(), self.mine)

    def test_half_a_download_is_refused(self):
        with open(os.path.join(self.updated, "dashboard_app.py"), "w",
                  encoding="utf-8") as f:
            f.write(self.GOOD)
        self.assertEqual(self.dash._code_to_run(), self.mine)

    def test_when_nothing_on_disk_starts_we_stay_put(self):
        """Both copies unusable: restarting would trade a working app for
        none, so it must refuse rather than exit."""
        self._fill(self.updated, self.BROKEN)
        self._fill(self.mine, self.BROKEN)
        self.assertEqual(self.dash._code_to_run(), "")
        with mock.patch("os.execv") as execv:
            with self.assertRaises(OSError):
                self.dash._relaunch_self()
        execv.assert_not_called()

    def test_the_listening_socket_is_not_handed_to_the_new_process(self):
        """The web server marks its socket inheritable so a reloader can pass
        the port along. Carried through a relaunch it makes the new process
        die on 'Address already in use'."""
        self._fill(self.updated, self.GOOD)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.set_inheritable(True)
        os.environ["WERKZEUG_SERVER_FD"] = str(sock.fileno())
        try:
            with mock.patch("os.execv") as execv:
                self.dash._relaunch_self()
            self.assertFalse(os.get_inheritable(sock.fileno()))
            self.assertNotIn("WERKZEUG_SERVER_FD", os.environ)
            execv.assert_called_once()
            self.assertIn(os.path.join(self.updated, "dashboard_app.py"),
                          execv.call_args[0][1])
        finally:
            os.environ.pop("WERKZEUG_SERVER_FD", None)
            sock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
