"""Cloud-mode security tests.

In cloud mode the dashboard is on the public internet holding live API keys,
so these check the things that must never regress: no unauthenticated access,
no "looks local so let it through" bypass, and brute-force throttling.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


class CloudModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-cloud-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        os.environ["SOLO_STUDIO_PASSWORD"] = PASSWORD
        import dashboard_app
        cls.dash = importlib.reload(dashboard_app)
        cls.dash.app.config["SESSION_COOKIE_SECURE"] = False  # test client is http
        cls.client = cls.dash.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        os.environ.pop("SOLO_STUDIO_PASSWORD", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        import dashboard_app
        importlib.reload(dashboard_app)  # restore local mode for other tests

    def setUp(self):
        self.dash._failures.clear()
        self.dash._global_failures.clear()
        with self.client.session_transaction() as s:
            s.clear()

    def test_cloud_mode_detected(self):
        self.assertTrue(self.dash.CLOUD_MODE)

    def test_localhost_is_not_trusted_in_cloud(self):
        """The critical one: a proxied request that looks local must NOT pass."""
        for path in ("/", "/setup", "/jarvis", "/jarvis/data", "/activity"):
            r = self.client.get(path, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"], path)

    def test_remote_requests_redirect_to_login(self):
        r = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_wrong_password_denied(self):
        r = self.client.post("/login", data={"password": "guess"})
        self.assertIn(b"Wrong password", r.data)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_correct_password_grants_access(self):
        r = self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/setup").status_code, 200)
        self.assertEqual(self.client.get("/jarvis").status_code, 200)

    def test_logout_revokes_access(self):
        self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/logout")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_brute_force_lockout(self):
        for _ in range(self.dash.MAX_FAILURES):
            self.client.post("/login", data={"password": "nope"},
                             environ_base={"REMOTE_ADDR": "198.51.100.7"})
        # Even the right password is refused once locked out.
        r = self.client.post("/login", data={"password": PASSWORD},
                             environ_base={"REMOTE_ADDR": "198.51.100.7"})
        self.assertIn(b"Too many attempts", r.data)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_lockout_is_per_ip(self):
        for _ in range(self.dash.MAX_FAILURES):
            self.client.post("/login", data={"password": "nope"},
                             environ_base={"REMOTE_ADDR": "198.51.100.8"})
        r = self.client.post("/login", data={"password": PASSWORD},
                             environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(r.status_code, 302)  # a different address is unaffected

    def test_global_lockout_survives_ip_spoofing(self):
        """Per-IP limits alone are dodgeable — a spoofed header must still lock."""
        with mock.patch("time.sleep"):  # skip the per-attempt delay
            for i in range(self.dash.GLOBAL_MAX_FAILURES):
                self.client.post(
                    "/login", data={"password": "nope"},
                    headers={"X-Forwarded-For": f"10.0.{i // 250}.{i % 250}"})
            r = self.client.post("/login", data={"password": PASSWORD},
                                 headers={"X-Forwarded-For": "10.9.9.9"})
        self.assertIn(b"Too many attempts", r.data)

    def test_health_and_icons_stay_public(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/icon-192.png").status_code, 200)
        self.assertEqual(self.client.get("/manifest.webmanifest").status_code, 200)

    def test_api_keys_never_rendered_back(self):
        """Setup must not echo stored secrets into the HTML."""
        import solo_studio_agent as core
        cfg = core.load_config()
        cfg.update(stripe_secret_key="sk_live_SECRET_VALUE_XYZ",
                   anthropic_api_key="sk-ant-SECRET_VALUE_XYZ")
        core.save_config(cfg)
        self.dash.STATE.reload()
        self.client.post("/login", data={"password": PASSWORD})
        body = self.client.get("/setup").data
        self.assertNotIn(b"SECRET_VALUE_XYZ", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WeakPasswordTest(unittest.TestCase):
    """A too-short deployment password must lock everyone out, not run wide open."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-weak-")
        os.environ["SOLO_STUDIO_HOME"] = cls.tmp
        os.environ["SOLO_STUDIO_PASSWORD"] = "short"
        import dashboard_app
        cls.dash = importlib.reload(dashboard_app)
        cls.dash.app.config["SESSION_COOKIE_SECURE"] = False
        cls.client = cls.dash.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SOLO_STUDIO_HOME", None)
        os.environ.pop("SOLO_STUDIO_PASSWORD", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        import dashboard_app
        importlib.reload(dashboard_app)

    def test_weak_password_refuses_everyone(self):
        r = self.client.post("/login", data={"password": "short"})
        self.assertEqual(r.status_code, 503)
        self.assertIn(b"longer password", r.data)
        self.assertEqual(self.client.get("/").status_code, 302)

