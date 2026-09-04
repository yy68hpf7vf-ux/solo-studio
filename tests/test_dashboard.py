"""Dashboard tests: the phone PIN gate must actually gate."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PhoneGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="solo-studio-dash-")
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

    def _set(self, **kv):
        cfg = self.core.load_config()
        cfg.update(kv)
        self.core.save_config(cfg)
        self.dash.STATE.reload()

    def remote(self, method, path, **kw):
        kw.setdefault("environ_base", {"REMOTE_ADDR": "10.0.0.9"})
        return getattr(self.client, method)(path, **kw)

    def test_local_always_allowed(self):
        r = self.client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)

    def test_remote_blocked_when_disabled(self):
        self._set(phone_access_enabled=False, phone_pin="")
        for path in ("/", "/jarvis", "/setup", "/jarvis/data"):
            self.assertEqual(self.remote("get", path).status_code, 403, path)

    def test_remote_blocked_without_pin_configured(self):
        self._set(phone_access_enabled=True, phone_pin="")
        self.assertEqual(self.remote("get", "/").status_code, 403)

    def test_pin_flow(self):
        self._set(phone_access_enabled=True, phone_pin="2468")
        r = self.remote("get", "/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/pin", r.headers["Location"])
        r = self.remote("post", "/pin", data={"pin": "0000"})
        self.assertIn(b"Wrong PIN", r.data)
        # Still locked after a wrong attempt.
        self.assertEqual(self.remote("get", "/jarvis").status_code, 302)
        r = self.remote("post", "/pin", data={"pin": "2468"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.remote("get", "/").status_code, 200)
        self.assertEqual(self.remote("get", "/jarvis").status_code, 200)

    def test_manifest_and_icons_public(self):
        self._set(phone_access_enabled=True, phone_pin="2468")
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.remote("get", "/manifest.webmanifest").status_code, 200)
        r = self.remote("get", "/icon-192.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:4], b"\x89PNG")

    def test_live_snapshot_shape(self):
        self._set(phone_access_enabled=False, phone_pin="")
        r = self.client.get("/live", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)
        for key in ("pending", "attention", "last_event"):
            self.assertIn(key, r.json)

    def test_live_requires_auth_from_other_devices(self):
        self._set(phone_access_enabled=True, phone_pin="2468")
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.remote("get", "/live").status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
