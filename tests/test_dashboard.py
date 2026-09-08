"""Dashboard tests: the phone PIN gate must actually gate."""

import os
import re
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

    def test_ask_requires_auth_from_other_devices(self):
        """The Ask page shows lead names and revenue — it must be gated."""
        self._set(phone_access_enabled=True, phone_pin="2468")
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.remote("get", "/ask").status_code, 302)
        self.assertEqual(
            self.remote("post", "/ask/send", json={"message": "hi"}).status_code, 302)
        self.assertEqual(self.remote("post", "/ask/clear").status_code, 302)

    # -- theme --------------------------------------------------------------

    @staticmethod
    def _lum(hex6):
        """Relative luminance 0..1, so the check is about how pale a colour is
        rather than which theme happens to be installed."""
        h = hex6.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255

    def _theme_bg(self):
        html = self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        m = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,6})", html)
        self.assertIsNotNone(m, "the shell must declare a --bg token")
        return m.group(1)

    def test_shell_is_dark(self):
        """Whatever palette is installed, the app is a dark app."""
        html = self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        self.assertIn("color-scheme:dark", html.replace(" ", ""))
        self.assertLess(self._lum(self._theme_bg()), 0.15)

    def test_no_page_paints_a_light_panel(self):
        """A pale surface anywhere would break the dark theme. Saturated accents
        are fine — this is about paleness, not brightness."""
        pale = re.compile(r"background(?:-color)?:\s*(#[0-9a-fA-F]{3,6})\b")
        for path in ("/", "/approve", "/team", "/activity", "/ask",
                     "/setup", "/updates"):
            html = self.client.get(
                path, environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
            # the QR code must stay light — phone cameras need the contrast
            html = re.sub(r"<svg\b.*?</svg>", "", html, flags=re.S)
            for colour in pale.findall(html):
                with self.subTest(page=path, colour=colour):
                    self.assertLess(self._lum(colour), 0.85,
                                    f"{path} paints a pale surface {colour}")

    def test_installed_icon_matches_the_app_theme(self):
        m = self.client.get("/manifest.webmanifest",
                            environ_base={"REMOTE_ADDR": "127.0.0.1"}).json
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["theme_color"], self._theme_bg())
        self.assertEqual(m["background_color"], self._theme_bg())

    def test_jarvis_reactor_reads_the_pipeline(self):
        """The outer ring is a gauge of real stages, not decoration."""
        html = self.client.get(
            "/jarvis", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        self.assertIn('id="gauge"', html)
        self.assertIn('id="gaugeTrack"', html)
        for stage in ("found", "contacted", "preview_sent",
                      "payment_link_sent", "paid", "delivered"):
            self.assertIn("'" + stage + "'", html)
        self.assertIn("your pipeline by stage", html)

    # -- getting it onto a phone ---------------------------------------------

    def test_setup_never_nests_a_form(self):
        """Browsers silently drop a form inside another form, so the button
        would render and do nothing. Keep every form top-level."""
        import re
        # turn on the branch that renders the restart button, or there is
        # nothing here to nest
        self._set(phone_access_enabled=True, phone_pin="4821")
        self.dash.BOUND_HOST = "127.0.0.1"
        html = self.client.get(
            "/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        depth = 0
        for tag in re.findall(r"<(/?)form\b", html):
            depth += -1 if tag == "/" else 1
            self.assertLessEqual(depth, 1, "a form is nested inside another form")
        self.assertEqual(depth, 0, "unbalanced form tags")

    def test_phone_offers_a_restart_when_not_yet_listening(self):
        self._set(phone_access_enabled=True, phone_pin="4821")
        self.dash.BOUND_HOST = "127.0.0.1"
        html = self.client.get(
            "/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        self.assertIn('form="phone-restart"', html)
        self.assertIn('id="phone-restart"', html)

    def test_phone_shows_the_qr_once_listening(self):
        self._set(phone_access_enabled=True, phone_pin="4821")
        self.dash.BOUND_HOST = "0.0.0.0"
        try:
            html = self.client.get(
                "/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
            self.assertNotIn('form="phone-restart"', html)
            self.assertIn("Add to Home Screen", html)
        finally:
            self.dash.BOUND_HOST = "127.0.0.1"

    def test_restart_without_a_launcher_relaunches_us(self):
        """With no launcher the button used to give up and tell the user to go
        quit the app by hand — on the one page whose whole job is not making
        them do that. Now the app brings itself back instead."""
        import threading
        came_back = threading.Event()
        real = self.dash._relaunch_self
        was = self.dash.LAUNCHER_RERUNS_US
        self.dash.LAUNCHER_RERUNS_US = False
        self.dash._relaunch_self = lambda: came_back.set()
        try:
            r = self.client.post("/action/restart", data={"back": "/setup"},
                                 environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("RESTARTING", r.data.decode())
            self.assertNotIn("Quit Solo Studio", r.data.decode())
            self.assertTrue(came_back.wait(5), "never relaunched")
        finally:
            self.dash._relaunch_self = real
            self.dash.LAUNCHER_RERUNS_US = was

    def test_nothing_ever_asks_the_user_to_quit_and_reopen(self):
        """The dead end this replaced. It should not come back."""
        import inspect
        self.assertNotIn("Quit Solo Studio", inspect.getsource(self.dash))

    # -- the backdrop, now that it stands still -----------------------------

    def test_no_page_carries_a_moving_backdrop(self):
        """It was built, then it was not wanted. Gone means gone — not left
        in the markup doing nothing."""
        for path in ("/", "/approve", "/team", "/activity", "/ask",
                     "/setup", "/updates", "/house", "/calls"):
            html = self.client.get(
                path, environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
            with self.subTest(page=path):
                self.assertNotIn('id="aurora"', html)
                self.assertNotIn("#aurora", html)
                self.assertNotIn("<canvas", html)
                self.assertNotIn("@keyframes drift", html)

    def test_nothing_animates_across_the_whole_screen(self):
        """The reason it was expensive: a full-screen layer that changes every
        frame. Nothing fixed and full-bleed may animate again."""
        import re
        html = self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        style = html[html.index("<style"):html.index("</style>")]
        for rule in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
            selector, body = rule[0].strip(), rule[1]
            if "position:fixed" in body and "inset:0" in body:
                with self.subTest(selector=selector[-60:]):
                    self.assertNotIn("animation", body)
                    self.assertNotIn("blur", body)

    def test_the_page_still_has_a_background_of_its_own(self):
        """Removing the light show must not leave a flat black page: the
        viewer paints its own ground behind anything transparent."""
        html = self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        self.assertIn("body::before", html)
        self.assertIn("gradient", html)

    def test_the_live_poll_does_not_call_into_what_was_removed(self):
        html = self.client.get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        self.assertNotIn("__solo", html)

    def test_live_feed_carries_what_the_backdrop_reacts_to(self):
        r = self.client.get("/live", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        for key in ("active", "revenue", "paid", "leads"):
            self.assertIn(key, r.json)

    def test_refresh_pill_ignores_the_new_live_fields(self):
        """The extra fields must not make every poll look like fresh activity."""
        snap = dict(self.dash._live_snapshot())
        before = self.dash._live_stamp(snap)
        snap.update(revenue=snap["revenue"] + 500, active=snap["active"] + 3,
                    paid=snap["paid"] + 1, leads=snap["leads"] + 9)
        self.assertEqual(before, self.dash._live_stamp(snap))

    # -- JARVIS ------------------------------------------------------------
    # The exit link once existed but was hidden by `.back{display:none}` in
    # the phone stylesheet, which left no way off the JARVIS screen on a
    # phone. These pin that down.

    def test_jarvis_has_a_way_back_to_the_dashboard(self):
        r = self.client.get("/jarvis", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertRegex(html, r'<a class="back" href="/"')

    # -- Setup walkthrough --------------------------------------------------

    def test_every_key_carries_directions(self):
        """Each API key must tell the user where to go and what to click."""
        import dashboard_app as dash
        self.assertTrue(dash.KEY_FIELDS)
        for spec in dash.KEY_FIELDS:
            with self.subTest(key=spec.get("field")):
                for required in ("field", "name", "job", "url", "site",
                                 "minutes", "steps"):
                    self.assertTrue(spec.get(required),
                                    f"{spec.get('field')} is missing {required}")
                self.assertTrue(spec["url"].startswith("https://"),
                                f"{spec['field']} link must be https")
                self.assertGreaterEqual(len(spec["steps"]), 2)

    def test_setup_lists_every_key_with_its_link(self):
        html = self.client.get(
            "/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        import dashboard_app as dash
        for spec in dash.KEY_FIELDS:
            self.assertIn(spec["url"], html)
            self.assertIn(f'name="{spec["field"]}"', html)

    def test_advanced_settings_still_save(self):
        """Fields moved into the Advanced block must still round-trip."""
        self.client.post("/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}, data={
            "anthropic_model": "claude-opus-5",
            "search_interval_hours": "36",
            "poll_interval_seconds": "90",
            "inkbox_agent_handle": "studio-bot",
        })
        cfg = self.core.load_config()
        self.assertEqual(cfg["search_interval_hours"], 36)
        self.assertEqual(cfg["poll_interval_seconds"], 90)
        self.assertEqual(cfg["inkbox_agent_handle"], "studio-bot")

    def test_blank_key_box_keeps_the_saved_key(self):
        """Submitting the form without retyping a key must not wipe it."""
        self._set(netlify_api_key="nfp_KEEP_ME")
        self.client.post("/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                         data={"netlify_api_key": "", "your_name": "Sam"})
        self.assertEqual(self.core.load_config()["netlify_api_key"], "nfp_KEEP_ME")

    def test_jarvis_exit_is_not_hidden_on_phones(self):
        html = self.client.get(
            "/jarvis", environ_base={"REMOTE_ADDR": "127.0.0.1"}).data.decode()
        for block in re.findall(r"@media[^{]*max-width[^{]*\{(.*?)\n\}", html, re.S):
            for rule in re.findall(r"\.back\b[^{}]*\{([^{}]*)\}", block):
                self.assertNotIn("display:none", rule.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
