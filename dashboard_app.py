"""Solo Studio dashboard — Flask web UI wrapping solo_studio_agent.

Run:  python3 dashboard_app.py [--open-browser]

Everything is configured on the Setup page (saved to config.json) — no code
editing, no environment variables. The dashboard binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import py_compile
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import timedelta

from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template_string, request, session, url_for)

import solo_studio_agent as core

PORT = 8747
HEALTH_MARKER = "solo-studio-dashboard"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reload()

    def reload(self):
        self.config = core.load_config()
        self.db = getattr(self, "db", None) or core.Database()
        self.services = core.Services(self.config)
        self.agent = core.Agent(self.db, self.services, self.config)


STATE = State()

# Cloud mode: set the SOLO_STUDIO_PASSWORD environment variable on the host to
# run this as an always-on server. Every request then requires that password —
# there is deliberately no "local request" bypass, because behind a hosting
# proxy every request can look local.
BOUND_HOST = "127.0.0.1"   # set in main(); 0.0.0.0 means the phone can reach us
RUN_PORT = PORT           # set in main(); what a relaunch should bind again
# Set by the launcher, which reruns us when we exit with the restart code and
# reinstalls any components an update needs on the way back. Nothing else may
# be read as a promise that something will bring us back: guessing from where
# the code sits got it wrong in both directions — telling users to go quit the
# app when a launcher was there, and quitting into nothing when it wasn't. When
# it is unset we relaunch ourselves instead, which works either way: replacing
# our own process keeps the same PID, so a launcher waiting on us never
# notices.
LAUNCHER_RERUNS_US = os.environ.get("SOLO_STUDIO_LAUNCHER") == "1"
CLOUD_PASSWORD = os.environ.get("SOLO_STUDIO_PASSWORD", "").strip()
CLOUD_MODE = bool(CLOUD_PASSWORD)
# Version this process started with — compared against what is installed
# on disk so we can tell the user a restart is needed.
RUNNING_SHA = core.installed_version().get("sha", "")
MIN_CLOUD_PASSWORD = 10
# Google Places Text Search: ~$32 per 1,000 calls, first 5,000 a month free,
# and one search pages up to three times.
PAGES_PER_SEARCH = 3
FREE_CALLS_MONTH = 5000
DOLLARS_PER_1K = 32


def _secret_key() -> bytes:
    """Stable signing key so logins survive restarts (kept beside config.json)."""
    path = os.path.join(core.app_data_dir(), "secret_key")
    try:
        with open(path, "rb") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    try:
        with open(path, "wb") as f:
            f.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass  # read-only disk: fall back to a per-process key
    return key


app = Flask(__name__)
app.secret_key = _secret_key()
app.json.sort_keys = False  # keep pipeline-stage order in /jarvis/data
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=CLOUD_MODE,      # cloud is HTTPS-only
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

ICON_192 = base64.b64decode("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")
ICON_512 = base64.b64decode("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")

PWA_META = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<link rel="apple-touch-icon" href="/icon-192.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="theme-color" content="#0b0912">'
)


def qr_svg(data: str, px: int = 190) -> str:
    """Inline SVG QR code for `data`, or "" if the qrcode library is absent
    (optional dependency — the URL is always shown as text as well)."""
    try:
        import qrcode
    except ImportError:
        return ""
    try:
        q = qrcode.QRCode(box_size=1, border=2)
        q.add_data(data)
        q.make(fit=True)
        matrix = q.get_matrix()
    except Exception:
        return ""
    n = len(matrix)
    rects = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
            f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges" '
            f'style="background:#fff;border-radius:8px">'
            f'<rect width="{n}" height="{n}" fill="#fff"/>'
            f'<g fill="#101826">{rects}</g></svg>')


def lan_ip() -> str:
    """This machine's address on the local network (best effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "this-mac"


def _is_local_request() -> bool:
    return (request.remote_addr or "") in ("127.0.0.1", "::1")


# Endpoints reachable without authentication (icons/manifest so the phone can
# install the app, health so the host can check the server is alive).
OPEN_ENDPOINTS = ("pin", "login", "manifest", "icon_png", "favicon",
                  "health")

# Simple brute-force throttle: {ip: [failure timestamps]}
_failures: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES = 8
LOCKOUT_SECONDS = 900


GLOBAL_MAX_FAILURES = 40  # backstop: client-supplied IPs can be spoofed
_global_failures: list[float] = []


def _throttled(ip: str) -> int:
    """Seconds the caller must wait, or 0 if they may try a password."""
    now = time.time()
    recent = [t for t in _failures[ip] if now - t < LOCKOUT_SECONDS]
    _failures[ip] = recent
    if len(recent) >= MAX_FAILURES:
        return int(LOCKOUT_SECONDS - (now - recent[0])) + 1
    _global_failures[:] = [t for t in _global_failures if now - t < LOCKOUT_SECONDS]
    if len(_global_failures) >= GLOBAL_MAX_FAILURES:
        return int(LOCKOUT_SECONDS - (now - _global_failures[0])) + 1
    return 0


def _record_failure(ip: str) -> None:
    now = time.time()
    _failures[ip].append(now)
    _global_failures.append(now)


def _clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
    _global_failures.clear()


def _password_too_weak() -> bool:
    return CLOUD_MODE and len(CLOUD_PASSWORD) < MIN_CLOUD_PASSWORD


@app.before_request
def _access_gate():
    """Two modes:

    Cloud (SOLO_STUDIO_PASSWORD set): this is a public server, so EVERY request
    needs the password. There is no local-request bypass on purpose — behind a
    hosting proxy, requests can arrive looking like they came from localhost.

    Mac (no password set): local requests pass freely; other devices on the
    Wi-Fi need phone access switched on plus the PIN.
    """
    if CLOUD_MODE:
        if request.endpoint in OPEN_ENDPOINTS:
            return None
        if session.get("cloud_ok"):
            return None
        return redirect(url_for("login"))

    if _is_local_request():
        return None
    cfg = STATE.config
    if not cfg.get("phone_access_enabled") or not cfg.get("phone_pin"):
        abort(403)
    if request.endpoint in OPEN_ENDPOINTS:
        return None
    if session.get("phone_ok"):
        return None
    return redirect(url_for("pin"))


# ---------------------------------------------------------------------------
# Background autopilot
# ---------------------------------------------------------------------------

def _autopilot_loop():
    # Straight to work on launch rather than waiting out the first interval.
    # keep_stocked() is already lazy — it does nothing unless the shelf is low
    # and it hasn't hunted recently — so opening the app twice in a row costs
    # nothing, and opening it after a week gets leads before you've sat down.
    last_run = 0.0
    try:
        time.sleep(2)                  # let the server finish binding
        STATE.agent.watch()
        if STATE.config.get("autopilot_enabled"):
            STATE.agent.crawl()
            STATE.agent.research_missing_emails()
    except Exception as e:
        try:
            STATE.db.log(None, "autopilot_error", core.explain(e, 500))
        except Exception:
            pass
    while True:
        time.sleep(5)
        try:
            cfg = STATE.config
            interval = max(30, int(cfg.get("poll_interval_seconds", 120)))
            if time.time() - last_run < interval:
                continue
            last_run = time.time()
            # The watchman runs even with the pipeline paused or half set up.
            # A stopped app can still be broken, and that is exactly when
            # nobody is looking at it.
            STATE.agent.watch()
            if cfg.get("autopilot_enabled") and cfg.get("inkbox_api_key"):
                STATE.agent.tick()
        except Exception as e:  # never let the worker die
            try:
                STATE.db.log(None, "autopilot_error", core.explain(e, 500))
            except Exception:
                pass


_autopilot_started = threading.Lock()


def start_autopilot_thread():
    """Start the background worker once; extra calls are no-ops."""
    if not _autopilot_started.acquire(blocking=False):
        return
    t = threading.Thread(target=_autopilot_loop, daemon=True,
                         name="solo-studio-autopilot")
    t.start()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solo Studio</title>
{{ pwa_meta|safe }}
<style>
/* Dark glass, after the Mac desktop: an indigo ground, a soft glow behind it,
   and translucent rounded panels floating on top. */
:root{
  /* Nocturne. The accent is pink, so the states that must never be confused —
     paid, waiting, broken — keep their own hues instead of all sliding into
     it; error is vermillion rather than red for exactly that reason. See the
     badge block below. */
  --bg:#0b0912; --bg2:#120e1c;
  --panel:rgba(26,20,42,.74);           /* dark glass — tinted, not transparent */
  --panel-2:rgba(26,20,42,.54);         /* one step quieter */
  --card:var(--panel);
  --ink:#f0ecf7; --mut:#9689ab;
  --line:rgba(190,170,255,.11);
  --line-2:rgba(190,170,255,.065);
  --acc:#f472b6;                        /* hot pink */
  --acc-ink:#26071a;
  --ok:#6ee7b7; --warn:#fcd34d; --bad:#ff7a5e;
  --r-lg:18px; --r-md:12px; --r-sm:9px;
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;min-height:100vh;color:var(--ink);background:var(--bg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;
  -webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:
    radial-gradient(58vw 42vw at 78% -6%, rgba(244,114,182,.11), transparent 62%),
    radial-gradient(46vw 38vw at 6% 96%, rgba(129,140,248,.10), transparent 66%),
    linear-gradient(180deg,var(--bg2),var(--bg) 52%)}

/* A still backdrop: one soft wash of colour behind the app, painted once by
   the compositor and never touched again. There was a drifting aurora here;
   it was asked for, then asked to go. */

/* Panels used to tilt toward the pointer and catch a moving highlight. Both
   are gone: nothing here moves because the mouse passed over it. */
.card{position:relative}

/* What JARVIS is doing right now. On every page, because the answer to "is it
   working or is it stuck" should never depend on which tab you are on. */
.working{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  margin:0 0 16px;padding:11px 15px;border-radius:var(--r-md);
  background:rgba(244,114,182,.08);border:1px solid rgba(244,114,182,.22)}
.working.done{background:var(--panel-2);border-color:var(--line)}
.working .spin{flex:0 0 auto;width:11px;height:11px;border-radius:50%;
  border:2px solid rgba(244,114,182,.3);border-top-color:var(--acc);
  animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.working .spin{animation:none}}

/* Narrowing what you're looking at, with the same three widths the crawler
   uses. A filter, not a delete — the leads stay. */
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.filters .chip{font-size:12.5px;padding:5px 12px;border-radius:999px;
  border:1px solid var(--line);color:var(--mut);text-decoration:none;
  background:var(--panel-2)}
.filters .chip:hover{color:var(--ink);border-color:rgba(244,114,182,.4)}
.filters .chip.on{background:var(--acc);color:var(--acc-ink);font-weight:600;
  border-color:transparent}

/* ---- chrome ---- */
header{position:sticky;top:0;z-index:40;display:flex;gap:22px;align-items:center;
  padding:13px 22px;color:var(--ink);
  background:rgba(13,10,22,.78);backdrop-filter:saturate(160%) blur(18px);
  -webkit-backdrop-filter:saturate(160%) blur(18px);
  border-bottom:1px solid var(--line-2)}
header .brand{font-weight:650;font-size:16px;letter-spacing:-.01em}
header a{color:#a397b8;text-decoration:none;font-weight:500;font-size:14px;
  padding:5px 2px;transition:color .15s}
header a:hover{color:var(--ink)}
main{max-width:1120px;margin:26px auto 60px;padding:0 18px}

.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r-lg);
  padding:20px 22px;margin-bottom:16px;
  backdrop-filter:blur(22px) saturate(150%);
  -webkit-backdrop-filter:blur(22px) saturate(150%);
  box-shadow:0 1px 0 rgba(210,195,255,.05) inset, 0 10px 34px rgba(0,0,0,.4)}
h1{font-size:20px;margin:0 0 14px;font-weight:620;letter-spacing:-.015em}
h2{font-size:15px;margin:0 0 10px;font-weight:600;letter-spacing:-.01em}

/* ---- tables ---- */
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 11px;border-bottom:1px solid var(--line-2);
  vertical-align:top}
tr:last-child td{border-bottom:0}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.07em}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(190,170,255,.03)}

/* ---- stage badges: lit glass, not pastel stickers ---- */
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
  font-weight:600;white-space:nowrap;border:1px solid transparent}
.b-found{background:rgba(244,114,182,.16);color:#f9a8d4;border-color:rgba(244,114,182,.32)}
.b-contacted{background:rgba(129,140,248,.16);color:#a5b4fc;border-color:rgba(129,140,248,.32)}
.b-preview_sent{background:rgba(252,211,77,.15);color:#fde68a;border-color:rgba(252,211,77,.3)}
.b-payment_link_sent{background:rgba(103,232,249,.14);color:#8fe6f5;border-color:rgba(103,232,249,.3)}
.b-paid,.b-delivered{background:rgba(110,231,183,.15);color:#8ff0cb;border-color:rgba(110,231,183,.3)}
.b-not_interested{background:rgba(190,170,255,.07);color:#9689ab;border-color:var(--line)}
.b-error{background:rgba(255,122,94,.16);color:#ffab94;border-color:rgba(255,122,94,.34)}
.b-building_preview,.b-sending_payment_link,.b-deploying_final{
  background:rgba(192,132,252,.15);color:#d8b4fe;border-color:rgba(192,132,252,.3)}

/* ---- controls ---- */
.btn{display:inline-block;border:1px solid var(--line);background:var(--panel);
  color:var(--ink);border-radius:var(--r-sm);padding:7px 13px;font-size:13px;
  font-weight:600;cursor:pointer;text-decoration:none;transition:.15s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.btn:hover{border-color:rgba(244,114,182,.5);color:#fff;
  background:rgba(244,114,182,.12)}
.btn-primary{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.btn-primary:hover{background:#f78fc6;border-color:#f78fc6;color:var(--acc-ink)}
.btn-danger{color:var(--bad)}
.btn-danger:hover{border-color:rgba(255,122,94,.5);background:rgba(255,122,94,.12);
  color:#ffb9a6}
.btn-sm{padding:4px 10px;font-size:12px}
form.inline{display:inline}
input[type=text],input[type=password],input[type=number],textarea,select{
  width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r-md);
  font:inherit;background:rgba(0,0,0,.28);color:var(--ink);transition:.15s}
input:focus,textarea:focus,select:focus{outline:0;border-color:rgba(244,114,182,.55);
  background:rgba(0,0,0,.38);box-shadow:0 0 0 3px rgba(244,114,182,.13)}
input::placeholder,textarea::placeholder{color:#6b6180}
textarea{min-height:120px;line-height:1.6}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 5px}
input[type=checkbox],input[type=radio]{accent-color:var(--acc);width:auto;
  transform:scale(1.1);vertical-align:-1px}
details summary::marker{color:var(--mut)}

/* ---- notices ---- */
.flash{padding:11px 15px;border-radius:var(--r-md);margin-bottom:14px;
  font-weight:500;border:1px solid transparent}
.flash.ok{background:rgba(110,231,183,.12);color:#8ff0cb;border-color:rgba(110,231,183,.28)}
.flash.err{background:rgba(255,122,94,.12);color:#ffb9a6;border-color:rgba(255,122,94,.3)}
.muted{color:var(--mut);font-size:13px}
.warnbar{background:rgba(252,211,77,.10);border:1px solid rgba(252,211,77,.28);
  color:#ffe9a3;border-radius:var(--r-lg);padding:13px 17px;margin-bottom:16px;
  display:flex;justify-content:space-between;align-items:center;gap:12px}
.attention{border-left:3px solid var(--warn)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 26px}

/* ---- stat tiles ---- */
.statrow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:var(--panel-2);border:1px solid var(--line);border-radius:var(--r-md);
  padding:12px 17px;text-align:center;min-width:98px;
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.stat b{display:block;font-size:21px;font-weight:640;letter-spacing:-.02em}
.stat span{font-size:11.5px;color:var(--mut);text-transform:uppercase;
  letter-spacing:.05em}

/* callout boxes + the cold-email preview, shared by several pages */
.note{border-radius:var(--r-md);padding:14px 16px;border:1px solid var(--line)}
.note.info{background:rgba(244,114,182,.10);border-color:rgba(244,114,182,.26)}
.note.warn{background:rgba(252,211,77,.11);border-color:rgba(252,211,77,.28)}
.note .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--acc);font-weight:600}
.emailbox{border:1px solid var(--line);border-radius:var(--r-md);padding:13px 15px;
  background:rgba(0,0,0,.24);margin:10px 0}
.emailbox .subj{font-weight:600;margin-bottom:7px}
.emailbox .body{white-space:pre-wrap;font-size:13.5px;color:#c3b8d6;line-height:1.6}

code{background:rgba(190,170,255,.09);padding:2px 6px;border-radius:5px;
  font-size:13px;color:#d6cce8;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--acc);text-decoration-color:rgba(244,114,182,.4);
  text-underline-offset:2px}
a:hover{text-decoration-color:currentColor}
td a{color:var(--ink);text-decoration:none;font-weight:600}
td a:hover{color:var(--acc)}
.tablewrap{overflow-x:auto}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:rgba(190,170,255,.16);border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:rgba(190,170,255,.26)}
::-webkit-scrollbar-track{background:transparent}

#live-pill{position:fixed;left:50%;transform:translateX(-50%);bottom:20px;
  background:rgba(28,22,44,.92);color:var(--ink);padding:11px 20px;
  border:1px solid var(--line);border-radius:999px;
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  font-size:14px;font-weight:600;cursor:pointer;z-index:50;
  box-shadow:0 8px 30px rgba(0,0,0,.5)}

@media (max-width:800px){
  .grid{grid-template-columns:1fr}
  header{padding:10px 14px;gap:14px;flex-wrap:wrap;font-size:14px}
  header .brand{font-size:16px;width:auto}
  main{margin:16px auto 50px;padding:0 12px}
  .card{padding:16px 16px;border-radius:15px}
  /* Stack the "needs an email" rows instead of squeezing them into columns */
  table.stack thead{display:none}
  table.stack tr{display:block;padding:10px 0;border-bottom:1px solid var(--line-2)}
  table.stack td{display:block;border:0;padding:2px 0}
  .btn{padding:9px 14px}
  input[type=text],input[type=password],input[type=number]{font-size:16px}
}
</style></head>
<body>
<header>
  <span class="brand">Solo Studio</span>
  <a href="{{ url_for('dashboard') }}">Dashboard</a>
  <a href="{{ url_for('approve_queue') }}">Approve{% if pending_count %}
    <span style="background:var(--warn);color:#1a1206;border-radius:999px;
    padding:1px 7px;font-size:12px;font-weight:700;margin-left:3px"
    >{{ pending_count }}</span>{% endif %}</a>
  <a href="{{ url_for('calls_page') }}">Calls{% if call_count %}
    <span style="background:var(--acc);color:var(--acc-ink);border-radius:999px;
    padding:1px 7px;font-size:12px;font-weight:700;margin-left:3px"
    >{{ call_count }}</span>{% endif %}</a>
  <a href="{{ url_for('house_page') }}">Studio</a>
  <a href="{{ url_for('team_page') }}">Team</a>
  <a href="{{ url_for('crawl_map') }}">Map</a>
  <a href="{{ url_for('activity') }}">Activity</a>
  <a href="{{ url_for('ask_page') }}">Ask</a>
  <a href="{{ url_for('setup') }}">Setup</a>
  <a href="{{ url_for('updates_page') }}">Updates{% if update_ready %}
    <span style="color:var(--ok)">●</span>{% endif %}</a>
  <a href="{{ url_for('jarvis') }}" style="margin-left:auto;color:#c4b5fd">◉ JARVIS</a>
  {% if cloud_mode %}<form class="inline" method="post" action="{{ url_for('logout') }}">
  <button class="btn btn-sm" style="background:transparent">Sign out</button>
  </form>{% endif %}
</header>
<main>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, m in messages %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% if job.running %}
<div class="working"><span class="spin" aria-hidden="true"></span>
  <b>JARVIS is {{ job.label }}.</b>
  <span class="muted">This page updates itself when he's done.</span></div>
{% elif job.summary %}
<div class="working done"><b>JARVIS:</b> <span>{{ job.summary }}</span></div>
{% endif %}
{% block body %}{% endblock %}
</main>
<div id="live-pill" hidden>New activity — tap to refresh</div>
<span id="live-stamp" hidden data-stamp="{{ live_stamp }}"></span>
<script>
/* Keeps ordinary pages current without throwing away anything you're typing:
   reloads on its own when idle, otherwise offers a tap-to-refresh pill. */
(function () {
  var stampEl = document.getElementById('live-stamp');
  var seen = stampEl ? stampEl.dataset.stamp : null;
  var pill = document.getElementById('live-pill');
  pill.onclick = function () { location.reload(); };

  function busy() {
    var el = document.activeElement;
    if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return true;
    var fields = document.querySelectorAll('input[type=text], input[type=email], textarea');
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].value && fields[i].value !== fields[i].defaultValue) return true;
    }
    return false;
  }

  function poll() {
    if (document.hidden) return;
    fetch('/live', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;                       /* signed out or offline */
        var stamp = d.last_event + ':' + d.pending + ':' + d.attention;
        if (!seen) { seen = stamp; return; }   /* no baseline: adopt this one */
        if (stamp === seen) return;
        if (busy()) { pill.hidden = false; }  /* don't wipe what you typed */
        else { location.reload(); }
      })
      .catch(function () {});
  }
  poll();
  setInterval(poll, 6000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) poll();
  });
})();
</script>
</body></html>
"""

DASHBOARD = """
{% extends "base" %}{% block body %}
<style>
.today{display:flex;gap:13px;align-items:flex-start;margin:2px 0 18px;
  padding-left:14px;border-left:2px solid rgba(244,114,182,.45)}
.today .mark{font-size:15px;line-height:1.3;color:var(--acc);opacity:.8;
  flex:0 0 auto}
.today p{margin:0;font-size:15.5px;line-height:1.5;color:var(--ink);
  font-style:italic;max-width:62ch}
.today cite{display:block;font-style:normal;font-size:12px;color:var(--mut);
  margin-top:4px;letter-spacing:.02em}
@media (max-width:800px){ .today p{font-size:15px} }

/* What JARVIS has noticed. Ordered worst first, so the top row is always the
   thing to do next. */
.watchcard{padding-top:14px}
.watchhead{display:flex;align-items:baseline;gap:11px;flex-wrap:wrap;
  margin-bottom:10px}
.watchhead h2{margin:0;font-size:15px;letter-spacing:.12em}
.watchhead .eye{width:9px;height:9px;border-radius:50%;background:var(--acc);
  box-shadow:0 0 0 3px rgba(244,114,182,.16);align-self:center}
.watchrow{display:flex;gap:11px;align-items:center;padding:9px 0;
  border-top:1px solid var(--line-2)}
.watchrow .dot{flex:0 0 auto;width:7px;height:7px;border-radius:50%;
  background:var(--mut)}
.watchrow.fix .dot{background:var(--bad)}
.watchrow.waiting .dot{background:var(--warn)}
.watchrow .t{font-weight:600}
.watchrow .btn{margin-left:auto;flex:0 0 auto}
@media (max-width:800px){
  .watchrow{flex-wrap:wrap}
  .watchrow .btn{margin-left:18px}
}
</style>
<div class="today">
  <span class="mark" aria-hidden="true">&ldquo;</span>
  <p>{{ today_line.text }}
    {% if today_line.source %}<cite>{{ today_line.source }}</cite>{% endif %}</p>
</div>
{% if not configured %}
<div class="warnbar"><div><b>Welcome!</b> Add your API keys on the Setup page to
get started — nothing works until then.</div>
<a class="btn btn-primary" href="{{ url_for('setup') }}">Open Setup</a></div>
{% endif %}

{% if findings %}
<div class="card watchcard">
  <div class="watchhead">
    <span class="eye" aria-hidden="true"></span>
    <h2>JARVIS</h2>
    <span class="muted">{{ findings|selectattr('level','equalto','fix')|list|length }}
      to fix · {{ findings|selectattr('level','equalto','waiting')|list|length }}
      waiting on you</span>
    <a class="muted" href="{{ url_for('jarvis') }}" style="margin-left:auto">Full view →</a>
  </div>
  {% for f in findings %}
  <div class="watchrow {{ f.level }}">
    <span class="dot" aria-hidden="true"></span>
    <div>
      <div class="t">{{ f.title }}</div>
      <div class="muted">{{ f.detail }}</div>
    </div>
    {% if f.cta %}<a class="btn btn-sm" href="{{ f.where }}">{{ f.cta }}</a>{% endif %}
  </div>
  {% endfor %}
</div>
{% endif %}

<div class="statrow">
  {% for s, n in stage_counts %}
  <div class="stat"><b>{{ n }}</b><span>{{ s.replace('_',' ') }}</span></div>
  {% endfor %}
</div>

{% if attention %}
<div class="card attention">
<h2>Needs your attention ({{ attention|length }})</h2>
<table><tbody>
{% for ev in attention %}
<tr>
  <td class="muted" style="white-space:nowrap">{{ ev['created_at'][:16].replace('T',' ') }}</td>
  <td>{% if ev['lead_id'] %}<a href="{{ url_for('lead_page', lead_id=ev['lead_id']) }}">
      lead #{{ ev['lead_id'] }}</a> — {% endif %}{{ ev['detail'] }}</td>
  <td style="text-align:right"><form class="inline" method="post"
      action="{{ url_for('resolve_event', event_id=ev['id']) }}">
      <button class="btn btn-sm">Done</button></form></td>
</tr>
{% endfor %}
</tbody></table></div>
{% endif %}

<div class="card">
<h2>Find new leads</h2>
<form method="post" action="{{ url_for('find_leads') }}" style="display:flex;gap:10px">
  <input type="text" name="query" required
    placeholder='A town — "Los Angeles, CA" — and JARVIS works out the rest'>
  <button class="btn btn-primary" style="white-space:nowrap">Find me leads</button>
</form>
<p class="muted" style="margin-bottom:0">Type just a place and JARVIS hunts it:
the towns around it, one trade at a time, until he has some. Name a trade too
(“plumbers in Riverside, CA”) and he runs that one first, then goes hunting
anyway if it turns up nothing. Only businesses with no website of their own are
kept — a Facebook page counts as no website. They need an email address added
before outreach can go out, and queue up on the
<a href="{{ url_for('approve_queue') }}">Approve</a> page.</p>
<div style="margin-top:10px">
<form class="inline" method="post" action="{{ url_for('run_searches') }}">
  <button class="btn">Run my saved searches now</button></form>
</div>
</div>

<div class="card">
<h2>Leads</h2>
{% if not leads %}<p class="muted">No leads yet — run a search above.</p>{% endif %}
{% if leads %}
<div class="tablewrap"><table>
<thead><tr><th>Business</th><th>Stage</th><th>Email</th><th>Links</th><th>Actions</th></tr></thead>
<tbody>
{% for l in leads %}
<tr {% if l['error'] %}style="background:rgba(255,122,94,.07)"{% endif %}>
  <td><a href="{{ url_for('lead_page', lead_id=l['id']) }}"><b>{{ l['name'] }}</b></a>
      <div class="muted">{{ l['category'] or '' }}{% if l['address'] %} · {{ l['address'] }}{% endif %}</div>
      {% if l['error'] %}<div class="muted" style="color:var(--bad)">⚠ {{ l['error'][:120] }}</div>{% endif %}</td>
  <td><span class="badge b-{{ l['stage'] }}">{{ l['stage'].replace('_',' ') }}</span>
      {% if l['do_not_contact'] %}<div class="muted">do not contact</div>{% endif %}</td>
  <td>{% if l['email'] %}{{ l['email'] }}{% else %}
      <form class="inline" method="post" action="{{ url_for('set_email', lead_id=l['id']) }}"
        style="display:flex;gap:6px">
        <input type="text" name="email" placeholder="add email…" style="min-width:150px">
        <button class="btn btn-sm">Save</button></form>{% endif %}</td>
  <td>{% if l['netlify_url'] %}<a href="{{ l['netlify_url'] }}" target="_blank">site</a>{% endif %}
      {% if l['stripe_session_url'] and l['stage'] == 'payment_link_sent' %}
      · <a href="{{ l['stripe_session_url'] }}" target="_blank">pay&nbsp;link</a>{% endif %}</td>
  <td style="white-space:nowrap">
    {% if l['stage'] == 'found' and l['email'] and not l['do_not_contact'] %}
      <form class="inline" method="post" action="{{ url_for('send_outreach', lead_id=l['id']) }}"
        onsubmit="return confirm('Send a REAL cold email to {{ l['email'] }}?')">
        <button class="btn btn-sm btn-primary">Send cold email</button></form>
    {% elif l['stage'] == 'contacted' %}
      <form class="inline" method="post" action="{{ url_for('advance', lead_id=l['id']) }}"
        onsubmit="return confirm('Design + deploy a watermarked preview and EMAIL the link to {{ l['email'] }}?')">
        <button class="btn btn-sm">Interested → build preview</button></form>
    {% elif l['stage'] == 'preview_sent' %}
      <form class="inline" method="post" action="{{ url_for('advance', lead_id=l['id']) }}"
        onsubmit="return confirm('Create a Stripe payment link and EMAIL it to {{ l['email'] }}?')">
        <button class="btn btn-sm">Send payment link</button></form>
    {% elif l['stage'] == 'error' %}
      <form class="inline" method="post" action="{{ url_for('retry_lead', lead_id=l['id']) }}">
        <button class="btn btn-sm">Retry</button></form>
    {% endif %}
    {% if l['stage'] in ('found','contacted','preview_sent','payment_link_sent') %}
      <form class="inline" method="post" action="{{ url_for('not_interested', lead_id=l['id']) }}">
        <button class="btn btn-sm btn-danger">✕</button></form>
    {% endif %}
  </td>
</tr>
{% endfor %}
</tbody></table></div>
{% endif %}
<div style="margin-top:12px;display:flex;gap:10px">
<form class="inline" method="post" action="{{ url_for('check_now') }}">
  <button class="btn">Check replies + payments now</button></form>
{% if config.autopilot_enabled %}
<form class="inline" method="post" action="{{ url_for('toggle_autopilot') }}">
  <button class="btn">Turn autopilot off</button></form>
{% endif %}
</div>
</div>
{% endblock %}
"""

LEAD_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>{{ lead['name'] }}
  <span class="badge b-{{ lead['stage'] }}">{{ lead['stage'].replace('_',' ') }}</span></h1>
<div class="grid">
  <div>
    <p class="muted" style="margin:4px 0">{{ lead['category'] or '' }}</p>
    <p style="margin:4px 0">{{ lead['address'] or '—' }}<br>
       {{ lead['phone'] or '' }}<br>
       {{ lead['email'] or 'no email yet' }}</p>
  </div>
  <div>
    {% if lead['netlify_url'] %}<p style="margin:4px 0">Site:
      <a href="{{ lead['netlify_url'] }}" target="_blank">{{ lead['netlify_url'] }}</a>
      {% if lead['stage'] not in ('delivered',) %}(watermarked preview){% endif %}</p>{% endif %}
    {% if lead['stripe_session_url'] %}<p style="margin:4px 0">Payment link:
      <a href="{{ lead['stripe_session_url'] }}" target="_blank">open</a></p>{% endif %}
    {% if lead['paid_at'] %}<p style="margin:4px 0">Paid: {{ lead['paid_at'][:16].replace('T',' ') }}</p>{% endif %}
    {% if lead['delivered_at'] %}<p style="margin:4px 0">Delivered: {{ lead['delivered_at'][:16].replace('T',' ') }}</p>{% endif %}
    {% if lead['error'] %}<p style="margin:4px 0;color:var(--bad)">⚠ {{ lead['error'] }}</p>{% endif %}
  </div>
</div>
<div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap">
  {% if lead['site_html'] %}
    <a class="btn" href="{{ url_for('lead_site_html', lead_id=lead['id']) }}" target="_blank">
      View generated HTML</a>{% endif %}
  {% if lead['stage'] == 'payment_link_sent' %}
    <form class="inline" method="post" action="{{ url_for('check_now') }}">
      <button class="btn">Check payment now</button></form>
    <form class="inline" method="post" action="{{ url_for('new_payment_link', lead_id=lead['id']) }}"
      onsubmit="return confirm('Only works if the old link expired. Create + EMAIL a fresh payment link?')">
      <button class="btn">Send new payment link</button></form>
  {% endif %}
</div>
</div>
<div class="card">
<h2>History</h2>
<table><tbody>
{% for ev in events %}
<tr><td class="muted" style="white-space:nowrap">{{ ev['created_at'][:16].replace('T',' ') }}</td>
    <td><code>{{ ev['kind'] }}</code></td><td>{{ ev['detail'] }}</td></tr>
{% endfor %}
</tbody></table>
</div>
{% endblock %}
"""

ACTIVITY = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Activity log</h1>
<table><tbody>
{% for ev in events %}
<tr><td class="muted" style="white-space:nowrap">{{ ev['created_at'][:16].replace('T',' ') }}</td>
    <td>{% if ev['lead_id'] %}<a href="{{ url_for('lead_page', lead_id=ev['lead_id']) }}">#{{ ev['lead_id'] }}</a>{% endif %}</td>
    <td><code>{{ ev['kind'] }}</code></td><td>{{ ev['detail'] }}</td></tr>
{% endfor %}
</tbody></table>
</div>
{% endblock %}
"""

SETUP = """
{% extends "base" %}{% block body %}
<style>
.keyhead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.keyhead .n{background:rgba(190,170,255,.14);color:var(--ink);border-radius:999px;
  width:22px;height:22px;
  display:inline-flex;align-items:center;justify-content:center;font-size:12px;
  font-weight:700;flex:0 0 auto;align-self:center}
.keyhead h3{margin:0;font-size:15px}
.pill{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;
  text-transform:uppercase;letter-spacing:.03em}
.pill.have{background:rgba(110,231,183,.17);color:#8ff0cb}
.pill.need{background:rgba(252,211,77,.17);color:#ffe9a3}
.keycard{border:1px solid var(--line);border-radius:var(--r-md);padding:15px 17px;
  margin-bottom:11px;background:var(--panel-2)}
.keycard.done{background:rgba(110,231,183,.06);border-color:rgba(110,231,183,.26)}
.keycard ol{margin:10px 0 0;padding-left:20px;font-size:13.5px;color:#c3b8d6}
.keycard ol li{margin-bottom:5px}
.keycard .note{font-size:12.5px;color:var(--mut);margin-top:9px;
  border-left:3px solid var(--line);padding-left:9px}
.progress{background:linear-gradient(135deg,rgba(244,114,182,.18),rgba(244,114,182,.06));
  border:1px solid rgba(244,114,182,.26);color:var(--ink);border-radius:var(--r-md);
  padding:13px 17px;margin-bottom:16px;font-weight:600}
.progress .sub{font-weight:400;opacity:.75;font-size:13px;margin-top:3px}
details.adv{border:1px solid var(--line);border-radius:var(--r-md);padding:0 15px;
  margin-top:16px;background:var(--panel-2)}
details.adv[open]{padding-bottom:12px}
details.adv summary{cursor:pointer;padding:12px 0;font-weight:600;font-size:14px}
</style>
<div class="card">
<h1>Setup</h1>

<div class="progress">
  {% if keys_missing %}{{ keys_have }} of {{ keys_needed }} keys saved —
    {{ keys_missing }} to go
    <div class="sub">Work down the list. Each one opens the right page for you.</div>
  {% else %}All {{ keys_needed }} keys saved
    <div class="sub">Hit <b>Test connections</b> at the bottom to check every one
      of them actually works.</div>
  {% endif %}
</div>

<form method="post">
<h2 style="margin-top:4px">API keys</h2>
<p class="muted" style="margin-top:-4px">A key is just a long password that lets
Solo Studio use a service on your behalf. Saved keys stay on this Mac and are
never shown again — leave a box blank to keep what's already saved.</p>

{% for k in key_fields %}
<div class="keycard {% if config[k.field] %}done{% endif %}">
  <div class="keyhead">
    <span class="n">{{ loop.index }}</span>
    <h3>{{ k.name }}</h3>
    {% if config[k.field] %}<span class="pill have">saved ✓</span>
    {% elif k.optional %}<span class="pill">optional</span>
    {% else %}<span class="pill need">needed</span>{% endif %}
    <span class="muted" style="margin-left:auto">{{ k.minutes }}</span>
  </div>
  <p class="muted" style="margin:5px 0 0">{{ k.job }}</p>
  <ol>{% for step in k.steps %}<li>{{ step|safe }}</li>{% endfor %}</ol>
  <div style="margin-top:11px">
    <a class="btn btn-primary" href="{{ k.url }}" target="_blank"
       rel="noopener noreferrer">Open {{ k.site }} →</a>
    {% if k.extra_url %}<a class="btn" href="{{ k.extra_url }}" target="_blank"
       rel="noopener noreferrer" style="margin-left:7px">{{ k.extra_label }} →</a>
    {% endif %}
  </div>
  <label>Paste the key here</label>
  <input type="password" name="{{ k.field }}" placeholder="{{ k.hint }}"
         autocomplete="off" spellcheck="false">
  {% if k.note %}<div class="note">{{ k.note }}</div>{% endif %}
</div>
{% endfor %}

<h2 style="margin-top:18px">Your business</h2>
<div class="grid">
<div>
<label>Your name</label>
<input type="text" name="your_name" value="{{ config.your_name }}">
<label>Studio name</label>
<input type="text" name="studio_name" value="{{ config.studio_name }}">
</div>
<div>
<label>Mailing address</label>
<input type="text" name="mailing_address" value="{{ config.mailing_address }}">
<p class="muted">Shown at the bottom of every cold email — US law requires a real
postal address on commercial email.</p>
<label>Website price (USD)</label>
<input type="number" name="site_price_usd" value="{{ price_value }}" min="1" step="1">
</div>
</div>
<h2 style="margin-top:18px">Automatic lead hunting</h2>

<div class="note info">
  <div class="k">Let it build the list for you</div>
  <p class="muted" style="margin:6px 0 10px">Say where you work and how far
  you'd travel. It looks up the real towns around you and writes a search for
  every trade in every town — you don't have to know the map.</p>
  <div class="grid" style="gap:0 18px">
    <div>
      <label>Your town</label>
      <input type="text" name="territory_base" form="build-searches"
        value="{{ config.territory_base }}" placeholder="Napanoch, NY">
    </div>
    <div>
      <label>How far you'd travel (miles)</label>
      <input type="number" name="territory_miles" form="build-searches"
        min="5" max="120" value="{{ config.territory_miles or 30 }}">
    </div>
  </div>
  <label>Trades to look for (one per line)</label>
  <textarea name="trades" form="build-searches" style="min-height:88px"
    >{{ trades_text }}</textarea>
  <p class="muted" style="margin:6px 0 0">These are trades where a lot of
  businesses still have no website. Restaurants and salons are left out on
  purpose — nearly all of them have one, so searching for them costs money to
  find nobody.</p>
  <button class="btn btn-primary" form="build-searches"
    style="margin-top:12px">Build my search list</button>
  <span class="muted" style="margin-left:10px">Replaces the list below.</span>
</div>

<div class="grid" style="margin-top:16px">
<div>
<label><input type="checkbox" name="auto_search_enabled" value="1"
  {% if config.auto_search_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Search for new leads automatically</label>
<label>Searches to run (one per line)</label>
<textarea name="saved_searches" style="min-height:90px"
  placeholder="plumbers in Riverside, CA&#10;barber shops in Riverside, CA&#10;landscapers in Corona, CA">{{ config.saved_searches }}</textarea>
</div>
<div>
<label>How many leads to bank</label>
<input type="number" name="lead_target" min="10" max="20000"
  value="{{ config.lead_target or 1000 }}">
<p class="muted">JARVIS sweeps the map around your town on his own — a few
spots every couple of minutes, no typing — until he has this many waiting.
Then he stops, because sweeping the same ground twice finds nothing and still
costs a search. He starts again if the pile runs down.</p>

<label>How wide to cast the net</label>
<select name="lead_quality">
  <option value="none" {% if config.lead_quality == 'none' %}selected{% endif %}>
    Strict — only businesses with no website at all</option>
  <option value="broken" {% if config.lead_quality in (None, '', 'broken') %}selected{% endif %}>
    Normal — also dead links, parked domains and social-only pages</option>
  <option value="weak" {% if config.lead_quality == 'weak' %}selected{% endif %}>
    Wide — also sites that are http-only or unusable on a phone</option>
</select>
<p class="muted">JARVIS opens every website Google lists and looks at it. A
dead link or a parked domain is a better lead than a blank listing — they
already paid for a site once. Costs nothing: these are ordinary web requests,
not Google searches. A site that loads, works and is built for phones is never
a lead at any setting.</p>

<label><input type="checkbox" name="auto_research_enabled" value="1"
  {% if config.auto_research_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Let the Researcher hunt missing emails</label>
<p class="muted">Uses Claude's web search to find each business's public contact
address. It only ever suggests — you accept or reject each one.</p>
<label>Searches per run</label>
<input type="number" name="searches_per_run" min="1" max="60"
  value="{{ config.searches_per_run or 20 }}">
<div class="note {{ 'warn' if cost.over else 'info' }}" style="margin-top:8px">
  <div class="k">What Google will charge you</div>
  <p class="muted" style="margin:5px 0 0">{{ cost.searches }} searches saved,
  {{ cost.per_run }} per run, every {{ cost.hours }}h — about
  <b>{{ cost.calls_month }} Google calls a month</b>.
  {% if cost.over %}That's over the 5,000 free ones; the overage runs roughly
  <b>${{ cost.dollars }}/month</b>. Lower "searches per run" or search less
  often.{% else %}The free allowance is 5,000 a month, so this costs you
  nothing. It works round the whole list over
  {{ cost.days_for_full_sweep }} day{{ '' if cost.days_for_full_sweep == 1
  else 's' }}.{% endif %}</p>
</div>
<div class="note info" style="margin-top:10px">
  <div class="k">What Claude will charge you</div>
  <p class="muted" style="margin:5px 0 0">
  Finding leads costs no Claude credit at all — that's Google and
  OpenStreetMap. Credit goes on <b>looking up email addresses</b>, and only
  after the free routes fail. Used this month:
  <b>{{ spend.lookups }} of {{ spend.lookup_cap }}</b> paid lookups
  (about ${{ spend.lookup_dollars }}), on {{ spend.model }}. It stops there;
  the free ones carry on.</p>
  <p class="muted" style="margin:6px 0 0">Google searches used this month:
  <b>{{ spend.google }} of {{ spend.google_cap }}</b> — the first 5,000 are
  free.</p>
</div>
<label>Paid address lookups a month</label>
<input type="number" name="monthly_lookup_cap" min="0" max="5000"
  value="{{ config.monthly_lookup_cap if config.monthly_lookup_cap is not none else 200 }}">
<p class="muted">Set it to 0 to never spend Claude credit on addresses at all —
the free ones (OpenStreetMap, reading their page) keep working.</p>

<label>Max cold emails per day</label>
<input type="number" name="daily_send_cap" min="1" max="200"
  value="{{ config.daily_send_cap }}">
<p class="muted">Found businesses wait on the <b>Approve</b> page — nothing is
emailed until you approve it. The daily cap protects your sending reputation;
sending hundreds a day gets a new mailbox flagged as spam.</p>
</div>
</div>
<h2 style="margin-top:18px">Your phone</h2>
<div class="grid">
<div>
{% if cloud_mode %}
<p class="muted">This copy runs in the cloud, so your phone can reach it from
anywhere — no Wi-Fi or Mac needed. On your phone open this same web address,
sign in, then tap <b>Share → Add to Home Screen</b> for an app icon.</p>
<div style="margin:10px 0">{{ phone_qr|safe }}</div>
<p class="muted">Scan to open it on your phone.</p>
{% else %}
<label><input type="checkbox" name="phone_access_enabled" value="1"
  {% if config.phone_access_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Let my phone open this dashboard (same Wi-Fi)</label>
<label>PIN for phone access (4–8 digits)</label>
<input type="text" name="phone_pin" value="{{ config.phone_pin }}" inputmode="numeric">
{% if not (config.phone_access_enabled and config.phone_pin) %}
  <p class="muted">Tick the box, pick a PIN, and click <b>Save settings</b>.
  A QR code appears here to set your phone up.</p>

{% elif not phone_listening %}
  <div class="note warn" style="margin-top:12px">
    <div style="font-weight:600">One restart and your phone can reach it</div>
    <div class="muted" style="margin-top:4px">Solo Studio only opens itself to
      your Wi-Fi when it starts up. It'll be back in a few seconds.</div>
    <button form="phone-restart" class="btn btn-primary"
      style="margin-top:10px">Restart Solo Studio</button>
  </div>

{% else %}
  <div style="display:flex;gap:16px;align-items:flex-start;margin-top:12px;
    flex-wrap:wrap">
    <div>{{ phone_qr|safe }}</div>
    <div class="muted" style="flex:1;min-width:215px">
      <ol style="margin:0;padding-left:18px;line-height:1.9">
        <li>Point your phone's camera at this code, tap the link.</li>
        <li>Enter your PIN: <b>{{ config.phone_pin }}</b></li>
        <li>Tap <b>Share</b>, then <b>Add to Home Screen</b>.</li>
      </ol>
      <p style="margin:10px 0 0">Or type
        <code style="font-size:15px">http://{{ lan_ip }}:8747</code>
        into your phone's browser.</p>
      <p style="margin:8px 0 0">Works while your phone is on the same Wi-Fi and
        Solo Studio is open on this Mac. For an icon that works anywhere,
        put it in the cloud — see the README.</p>
    </div>
  </div>
{% endif %}
{% endif %}
</div>
<div>
<label><input type="checkbox" name="ntfy_enabled" value="1"
  {% if config.ntfy_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Push notifications to my phone (free ntfy app)</label>
{% if config.ntfy_topic %}
<p class="muted">1. Install <b>ntfy</b> from the App Store.<br>
2. In ntfy tap <b>+</b> and subscribe to this exact topic:<br>
<code>{{ config.ntfy_topic }}</code><br>
3. You'll get a buzz for replies, previews, and payments.
<a href="{{ url_for('test_notification') }}">Send a test notification</a></p>
{% else %}<p class="muted">Tick the box and Save — a private topic name will be
created for you, with instructions here.</p>{% endif %}
</div>
</div>
<h2 style="margin-top:18px">Cold email template</h2>
<p class="muted">Placeholders: {lead_name} {your_name} {studio_name} {price} {mailing_address}</p>
<label>Subject</label>
<input type="text" name="outreach_subject" value="{{ config.outreach_subject }}">
<label>Body</label>
<textarea name="outreach_body">{{ config.outreach_body }}</textarea>
<details class="adv">
<summary>Advanced — you almost certainly don't need these</summary>
<p class="muted">Sensible defaults are already in place. Changing these can stop
things working, so only touch them if something specific pushed you here.</p>
<div class="grid">
<div>
<label>Inkbox agent handle</label>
<input type="text" name="inkbox_agent_handle" value="{{ config.inkbox_agent_handle }}"
  placeholder="blank = detect it automatically">
<p class="muted">Only needed if your Inkbox account has more than one identity.</p>
<label>Claude model</label>
<input type="text" name="anthropic_model" value="{{ config.anthropic_model }}">
<p class="muted">A wrong name here breaks replies, previews and research.</p>
</div>
<div>
<label>How often to hunt for new leads (hours)</label>
<input type="number" name="search_interval_hours" min="1" max="168"
  value="{{ config.search_interval_hours }}">
<label>Background check interval (seconds)</label>
<input type="number" name="poll_interval_seconds"
  value="{{ config.poll_interval_seconds }}" min="30" step="10">
<p class="muted">How often the app looks for new replies and payments.
Lower is not better — it just uses more of your API allowance.</p>
</div>
</div>
</details>

<div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap">
<button class="btn btn-primary">Save settings</button>
<a class="btn" href="{{ url_for('setup_test') }}">Test connections</a>
</div>
</form>

<!-- outside the settings form on purpose: a nested form is dropped by browsers -->
<form id="phone-restart" method="post" action="{{ url_for('do_restart') }}">
  <input type="hidden" name="back" value="{{ url_for('setup') }}">
</form>
<form id="build-searches" method="post"
      action="{{ url_for('build_searches') }}"></form>
</div>
{% endblock %}
"""

MAP_PAGE = """
{% extends "base" %}{% block body %}
<style>
.mapwrap{position:relative;background:var(--panel-2);border:1px solid var(--line);
  border-radius:var(--r-lg);padding:10px;overflow:hidden}
#usmap{display:block;width:100%;height:auto}
#usmap .state{fill:rgba(140,120,210,.10);stroke:rgba(190,170,255,.30);
  stroke-width:.7;stroke-linejoin:round}
#usmap .dot{stroke:none}
#usmap .dot.done{fill:rgba(167,139,250,.55)}
#usmap .dot.empty{fill:rgba(150,137,171,.35)}
#usmap .dot.now{fill:var(--acc)}
#usmap .ping{fill:none;stroke:var(--acc);stroke-width:1.2;opacity:.9}
#usmap text{font:9px -apple-system,sans-serif;fill:var(--mut)}
#usmap text.here{fill:var(--ink);font-weight:600;font-size:11px}
.mapbar{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline;margin-bottom:12px}
.mapbar b{font-size:26px;letter-spacing:-.02em}
.mapkey{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;font-size:12.5px;
  color:var(--mut)}
.mapkey i{display:inline-block;width:9px;height:9px;border-radius:50%;
  margin-right:5px;vertical-align:-1px;font-style:normal}
@keyframes ping{0%{r:4;opacity:.9}100%{r:22;opacity:0}}
#usmap .ping{animation:ping 2.2s ease-out infinite}
@media (prefers-reduced-motion:reduce){ #usmap .ping{animation:none;opacity:.35}}
</style>
<div class="card">
  <div class="mapbar">
    <div><b id="m-found">0</b><div class="muted">leads found</div></div>
    <div><b id="m-cities">0</b><div class="muted">cities swept</div></div>
    <div style="margin-left:auto;text-align:right">
      <div id="m-here" style="font-weight:600">—</div>
      <div class="muted" id="m-progress"></div>
    </div>
  </div>
  <div class="mapwrap">
    <svg id="usmap" viewBox="0 0 960 600" role="img"
         aria-label="Where JARVIS has searched"></svg>
  </div>
  <script type="application/json" id="us-outline">{{ outline|safe }}</script>
  <div class="mapkey">
    <span><i style="background:var(--acc)"></i>searching now</span>
    <span><i style="background:rgba(167,139,250,.55)"></i>swept, leads found</span>
    <span><i style="background:rgba(150,137,171,.35)"></i>swept, nothing to pitch</span>
    <span id="m-note"></span>
  </div>
  <p class="muted" style="margin-bottom:0">Every dot is a real place JARVIS
  looked, at the coordinates Google gave for it. Cities he hasn't reached yet
  aren't drawn, because he hasn't looked them up — the map fills in as he
  works.</p>
</div>
<script>
(function () {
  var svg = document.getElementById('usmap');
  var NS = 'http://www.w3.org/2000/svg';
  var W = 960, H = 600, PAD = 18;
  var OUTLINE = JSON.parse(document.getElementById('us-outline').textContent);
  var proj = null;

  /* One projection for the coastline and the dots alike, worked out from the
     outline's own bounds — so a city can never land in the wrong state. */
  function fit(b) {
    var sx = (W - 2 * PAD) / ((b.e - b.w) * b.aspect),
        sy = (H - 2 * PAD) / (b.n - b.s),
        k = Math.min(sx, sy),
        w = (b.e - b.w) * b.aspect * k, h = (b.n - b.s) * k;
    return {
      x: function (lng) { return PAD + (W - 2 * PAD - w) / 2 + (lng - b.w) * b.aspect * k; },
      y: function (lat) { return PAD + (H - 2 * PAD - h) / 2 + (b.n - lat) * k; }
    };
  }

  function coastline() {
    OUTLINE.forEach(function (state) {
      state.forEach(function (ring) {
        var d = '';
        for (var i = 0; i < ring.length; i += 2) {
          d += (i ? 'L' : 'M') + proj.x(ring[i]).toFixed(1) + ' ' +
               proj.y(ring[i + 1]).toFixed(1);
        }
        svg.appendChild(el('path', {class: 'state', d: d + 'Z'}));
      });
    });
  }

  function el(name, attrs, text) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  function draw(d) {
    var b = d.bounds;
    svg.textContent = '';
    proj = fit(b);
    coastline();

    var off = 0;
    d.points.forEach(function (p) {
      if (p.lng < b.w || p.lng > b.e || p.lat < b.s || p.lat > b.n) { off++; return; }
      var x = proj.x(p.lng), y = proj.y(p.lat);
      var r = p.found ? Math.min(11, 3 + Math.sqrt(p.found)) : 2.5;
      var cls = p.now ? 'now' : (p.found ? 'done' : 'empty');
      if (p.now) svg.appendChild(el('circle', {class: 'ping', cx: x, cy: y, r: 4}));
      var dot = el('circle', {class: 'dot ' + cls, cx: x, cy: y, r: r});
      dot.appendChild(el('title', {}, p.city + ' — ' +
        (p.found ? p.found + ' leads' : 'nothing worth pitching')));
      svg.appendChild(dot);
      if (p.now || p.found >= 8) {
        svg.appendChild(el('text', {x: x + r + 4, y: y + 3,
                                    class: p.now ? 'here' : ''}, p.city));
      }
    });
    document.getElementById('m-note').textContent =
      off ? off + ' outside the map (Alaska, Hawaii)' : '';
    document.getElementById('m-found').textContent = d.found.toLocaleString();
    document.getElementById('m-cities').textContent =
      d.points.length.toLocaleString();
    document.getElementById('m-here').textContent =
      d.working ? (d.here || 'starting up') : 'JARVIS is switched off';
    document.getElementById('m-progress').textContent =
      d.of ? 'city ' + d.at + ' of ' + d.of.toLocaleString() : '';
  }

  function poll() {
    fetch('/map/data', {cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) draw(d); })
      .catch(function () {});
  }
  poll();
  setInterval(function () { if (!document.hidden) poll(); }, 5000);
})();
</script>
{% endblock %}
"""


ASK = """
{% extends "base" %}{% block body %}
<style>
.chat{display:flex;flex-direction:column;gap:12px;min-height:46vh;
  max-height:62vh;overflow-y:auto;padding:4px 2px 8px}
.msg{max-width:min(720px,86%);padding:10px 14px;border-radius:13px;
  white-space:pre-wrap;line-height:1.5;overflow-wrap:anywhere}
.msg.you{align-self:flex-end;background:var(--acc);color:var(--acc-ink);
  font-weight:500;border-bottom-right-radius:5px}
.msg.bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);
  border-bottom-left-radius:5px}
.msg.bad{align-self:flex-start;background:rgba(255,122,94,.12);color:#ffb9a6;
  border:1px solid rgba(255,122,94,.3)}
.msg.think{align-self:flex-start;background:var(--panel);color:var(--mut);
  border:1px solid var(--line)}
.askbar{display:flex;gap:8px;margin-top:12px;align-items:flex-end}
.askbar button{flex:0 0 auto;padding:11px 20px}
.askbar textarea{min-height:46px;max-height:150px;resize:vertical;flex:1}
.starters{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.starters button{font:inherit;font-size:13px;background:var(--panel);cursor:pointer;
  border:1px solid var(--line);border-radius:999px;padding:7px 14px;color:var(--ink);
  transition:.15s}
.starters button:hover{border-color:rgba(244,114,182,.5);color:#fff;
  background:rgba(244,114,182,.13)}
@media (max-width:800px){.chat{max-height:none}.msg{max-width:92%}}
</style>
<div class="card">
<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
  <h1 style="margin:0">Ask</h1>
  <span class="muted">Knows your leads, your numbers and how the app works.</span>
  <form method="post" action="{{ url_for('ask_clear') }}" style="margin-left:auto">
    <button class="btn btn-sm">Clear chat</button></form>
</div>

{% if not has_key %}
<div class="warnbar" style="margin-top:14px">
  <span>Add your <b>Anthropic (Claude)</b> key on Setup and I can start
  answering — it's the same key the app uses to design sites.</span>
  <a class="btn" href="{{ url_for('setup') }}">Go to Setup</a>
</div>
{% endif %}

<div class="chat" id="chat">
{% if not history %}
  <div class="msg bot">Hi{% if config.your_name %} {{ config.your_name.split()[0] }}{% endif %},
I'm built into Solo Studio, so I can see your pipeline as it stands right now.
Ask me anything about your leads, your setup, or what to do next.

I can't send emails or take payments myself — I'll tell you which button to
press for that.</div>
{% endif %}
{% for m in history %}
  <div class="msg {{ 'you' if m.role == 'user' else 'bot' }}">{{ m.content }}</div>
{% endfor %}
</div>

<div class="starters" id="starters">
  <button type="button">What should I do next?</button>
  <button type="button">How's my pipeline looking?</button>
  <button type="button">Is anything stuck or waiting on me?</button>
  <button type="button">Walk me through my next API key</button>
</div>

<form class="askbar" id="askform">
  <textarea id="q" placeholder="Ask anything…" autocomplete="off"></textarea>
  <button class="btn btn-primary" id="send">Send</button>
</form>
</div>

<script>
(function(){
  var chat = document.getElementById('chat'),
      form = document.getElementById('askform'),
      box  = document.getElementById('q'),
      send = document.getElementById('send'),
      starters = document.getElementById('starters');

  function bubble(cls, text){
    var d = document.createElement('div');
    d.className = 'msg ' + cls;
    d.textContent = text;
    chat.appendChild(d);
    chat.scrollTop = chat.scrollHeight;
    return d;
  }

  function ask(text){
    text = (text || '').trim();
    if (!text || send.disabled) return;
    bubble('you', text);
    box.value = '';
    starters.style.display = 'none';
    send.disabled = true;
    var thinking = bubble('think', 'Thinking…');
    fetch({{ url_for('ask_send')|tojson }}, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    })
    .then(function(r){ return r.json(); })
    .then(function(d){
      thinking.remove();
      bubble(d.ok ? 'bot' : 'bad', d.ok ? d.reply : (d.error || 'Something went wrong.'));
    })
    .catch(function(){
      thinking.remove();
      bubble('bad', "I couldn't reach Claude just then. Check your internet and try again.");
    })
    .finally(function(){ send.disabled = false; box.focus(); });
  }

  form.addEventListener('submit', function(e){ e.preventDefault(); ask(box.value); });
  box.addEventListener('keydown', function(e){          // Enter sends, Shift+Enter newline
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(box.value); }
  });
  starters.addEventListener('click', function(e){
    if (e.target.tagName === 'BUTTON') ask(e.target.textContent);
  });
  chat.scrollTop = chat.scrollHeight;
})();
</script>
{% endblock %}
"""


HOUSE = """
{% extends "base" %}{% block body %}
<style>
.house{--wall:rgba(190,170,255,.14);--warm:#f472b6;
  position:relative;border:1px solid var(--wall);border-radius:20px;
  background:linear-gradient(180deg,rgba(32,24,52,.55),rgba(16,12,26,.72));
  padding:16px;overflow:hidden}
.floor{display:grid;gap:12px;margin-bottom:12px}
.floor.three{grid-template-columns:repeat(3,1fr)}
.floor.two{grid-template-columns:repeat(2,1fr)}
.floor.gate{grid-template-columns:1fr}
.floor:last-child{margin-bottom:0}
.storey{display:flex;align-items:center;gap:10px;margin:2px 0 9px;
  font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--mut)}
.storey::after{content:"";flex:1;height:1px;background:var(--wall)}

/* ---- a room ---- */
.room{position:relative;border:1px solid var(--wall);border-radius:13px;
  padding:13px 14px 12px;min-height:118px;overflow:hidden;
  background:rgba(22,17,36,.62);transition:border-color .4s,background .4s}
.room .lamp{position:absolute;inset:-40% -10% auto -10%;height:150%;
  pointer-events:none;opacity:.5;
  background:radial-gradient(60% 55% at 50% 0%,var(--tint),transparent 70%)}
.room.on{border-color:rgba(244,114,182,.3)}
.room.on .lamp{animation:breathe 7s ease-in-out infinite}
.room.standby .lamp{opacity:.16}
.room.nokey{background:rgba(16,12,10,.7)}
.room.nokey .lamp{opacity:.08}
@keyframes breathe{0%,100%{opacity:.4}50%{opacity:.72}}
.room .who{position:relative;display:flex;align-items:center;gap:8px}
.room .who .ico{font-size:17px;line-height:1}
.room .who b{font-size:13.5px;font-weight:600}
.room .doing{position:relative;font-size:11px;color:var(--mut);margin-top:3px;
  letter-spacing:.02em}
.room .tag{position:absolute;top:11px;right:12px;font-size:9px;font-weight:700;
  letter-spacing:.11em;padding:2px 7px;border-radius:999px}
.room.on .tag{background:rgba(110,231,183,.16);color:#8ff0cb}
.room.standby .tag{background:rgba(190,170,255,.09);color:var(--mut)}
.room.nokey .tag{background:rgba(255,122,94,.16);color:#ffab94}
/* the leads currently standing in this room */
.pen{position:relative;display:flex;align-items:flex-end;gap:5px;
  margin-top:11px;min-height:22px;flex-wrap:wrap}
.mote{width:9px;height:9px;border-radius:50%;background:var(--warm);
  box-shadow:0 0 9px var(--warm),0 0 18px rgba(244,114,182,.5);
  animation:bob 3.4s ease-in-out infinite}
.mote:nth-child(2){animation-delay:-.5s}.mote:nth-child(3){animation-delay:-1s}
.mote:nth-child(4){animation-delay:-1.5s}.mote:nth-child(5){animation-delay:-2s}
.mote:nth-child(6){animation-delay:-2.6s}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.pen .more{font-size:10.5px;color:var(--mut);align-self:center}
.pen .empty{font-size:10.5px;color:var(--mut);opacity:.6}
.room .num{position:absolute;right:12px;bottom:10px;font-size:26px;
  font-weight:650;letter-spacing:-.03em;color:var(--ink);opacity:.16;
  transition:opacity .25s}
.room.has{cursor:pointer}
.room.has:hover{border-color:rgba(244,114,182,.4);
  box-shadow:0 0 0 1px rgba(244,114,182,.14),0 14px 40px rgba(0,0,0,.4)}
.room.has:hover .num{opacity:.3}
.room:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
/* who is actually standing in this room */
.guests{position:relative;margin-top:10px;padding-top:9px;
  border-top:1px solid var(--line-2);display:none}
.room.open .guests{display:block}
.room.open .pen{display:none}
.guests div{font-size:11.5px;color:var(--ink);padding:2px 0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.guests .more{color:var(--mut)}

/* ---- your desk: the one room the work can't get past on its own ---- */
.desk{border:1px solid rgba(252,211,77,.32);border-radius:13px;padding:14px 16px;
  background:linear-gradient(90deg,rgba(252,211,77,.11),rgba(252,211,77,.04));
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.desk .ico{font-size:19px}
.desk b{font-size:14px}
.desk .sub{font-size:11.5px;color:var(--mut);margin-top:2px}
.desk .go{margin-left:auto}
.desk.clear{border-color:var(--line);
  background:linear-gradient(90deg,rgba(190,170,255,.05),transparent)}

/* ---- the vault, where it all ends up ---- */
.vault{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.vbox{border:1px solid var(--wall);border-radius:13px;padding:13px 14px;
  background:rgba(22,17,36,.62)}
.vbox b{display:block;font-size:23px;font-weight:650;letter-spacing:-.02em}
.vbox.paid b{color:#8ff0cb}
.vbox span{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--mut)}

/* ---- a lead moving between rooms ---- */
#traffic{position:absolute;inset:0;pointer-events:none;z-index:5}
.runner{position:absolute;width:10px;height:10px;border-radius:50%;
  background:#dbd9ff;box-shadow:0 0 12px #f472b6,0 0 26px rgba(244,114,182,.7)}

@media (max-width:800px){
  .floor.three,.vault{grid-template-columns:1fr 1fr}
  .room{min-height:104px}
  .desk .go{margin-left:0;width:100%}
}
@media (prefers-reduced-motion:reduce){
  .room.on .lamp,.mote{animation:none}
}
</style>

<div class="card">
<div style="display:flex;align-items:baseline;gap:11px;flex-wrap:wrap">
  <h1 style="margin:0">The studio</h1>
  <span class="muted">Every lead you have, in the room it's sitting in right now.</span>
  <a class="btn btn-sm" style="margin-left:auto" href="{{ url_for('team_page') }}">
    What each one does</a>
</div>

<div class="house" style="margin-top:14px">
  <div id="traffic"></div>

  <div class="storey">Top floor · finding them</div>
  <div class="floor three" id="floor-3"></div>

  <div class="storey">The landing · your call</div>
  <div class="floor gate"><div class="desk" id="desk"></div></div>

  <div class="storey">First floor · winning them</div>
  <div class="floor three" id="floor-1"></div>

  <div class="storey">Ground floor · getting paid</div>
  <div class="floor two" id="floor-0"></div>

  <div class="storey">The vault</div>
  <div class="vault" id="vault"></div>
</div>
<p class="muted" style="margin:12px 0 0">Each dot is one real business.
<b>Tap any room</b> to see who's in it. They drift down through the house as the
work gets done — and nothing gets past the landing without you.</p>
</div>

<script>
(function () {
  var ROOMS = {{ rooms|tojson }};
  var byKey = {};
  ROOMS.forEach(function (r) { byKey[r.key] = r; });
  var last = null, slow = window.matchMedia
    && matchMedia('(prefers-reduced-motion: reduce)').matches;

  var openRooms = {};

  function guests(live) {
    var names = live.names || [];
    if (!names.length) return '<div class="more">nobody in here</div>';
    var out = names.map(function (n) { return '<div>' + n + '</div>'; }).join('');
    if (live.count > names.length) {
      out += '<div class="more">and ' + (live.count - names.length) + ' more</div>';
    }
    return out;
  }

  function motes(n) {
    if (!n) return '<span class="empty">empty</span>';
    var show = Math.min(n, 6), out = '';
    for (var i = 0; i < show; i++) out += '<span class="mote"></span>';
    if (n > show) out += '<span class="more">+' + (n - show) + '</span>';
    return out;
  }

  function drawRooms(rooms) {
    [3, 1, 0].forEach(function (storey) {
      var host = document.getElementById('floor-' + storey);
      host.innerHTML = ROOMS.filter(function (r) { return r.storey === storey; })
        .map(function (spec) {
          var live = rooms[spec.key] || { count: 0, state: 'standby', note: '' };
          var tag = live.state === 'on' ? 'ON DUTY'
                  : live.state === 'nokey' ? 'NEEDS KEY' : 'STANDBY';
          var open = openRooms[spec.key] ? ' open' : '';
          var has = live.count ? ' has' : '';
          return '<div class="room ' + live.state + has + open + '"'
            + ' id="room-' + spec.key + '"'
            + (live.count ? ' tabindex="0" role="button"' : '')
            + ' style="--tint:' + spec.tint + '">'
            + '<span class="lamp"></span>'
            + '<span class="tag">' + tag + '</span>'
            + '<div class="who"><span class="ico">' + spec.icon + '</span>'
            + '<b>' + spec.name + '</b></div>'
            + '<div class="doing">' + (live.note || spec.doing) + '</div>'
            + '<div class="pen">' + motes(live.count) + '</div>'
            + '<div class="guests">' + guests(live) + '</div>'
            + '<span class="num">' + live.count + '</span></div>';
        }).join('');
    });
  }

  function drawDesk(you) {
    var d = document.getElementById('desk'), n = you.waiting;
    d.className = 'desk' + (n ? '' : ' clear');
    d.innerHTML = '<span class="ico">🪑</span><div><b>'
      + (n ? n + (n === 1 ? ' business is' : ' businesses are') + ' waiting on you'
           : 'Nothing waiting on you')
      + '</b><div class="sub">'
      + (n ? 'Read the email, tap approve, and it carries on downstairs.'
           : 'Every draft has been through you.')
      + '</div></div>'
      + (n ? '<a class="btn btn-primary go" href="{{ url_for('approve_queue') }}">'
           + 'Open Approve</a>' : '');
  }

  function drawVault(v) {
    document.getElementById('vault').innerHTML =
      '<div class="vbox paid"><b>$' + v.collected.toLocaleString()
        + '</b><span>Collected</span></div>'
    + '<div class="vbox"><b>$' + v.pending.toLocaleString()
        + '</b><span>Still owed</span></div>'
    + '<div class="vbox"><b>' + v.delivered + '</b><span>Sites live</span></div>';
  }

  /* When a room's queue shrinks and the next one grows, something walked
     between them — send a light along that path so you can see it happen. */
  function runBetween(fromKey, toKey) {
    if (slow) return;
    var a = document.getElementById('room-' + fromKey),
        b = document.getElementById('room-' + toKey),
        stage = document.getElementById('traffic');
    if (!a || !b || !stage) return;
    var box = stage.getBoundingClientRect(),
        ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(),
        x1 = ra.left - box.left + ra.width / 2, y1 = ra.top - box.top + ra.height / 2,
        x2 = rb.left - box.left + rb.width / 2, y2 = rb.top - box.top + rb.height / 2;
    var dot = document.createElement('span');
    dot.className = 'runner';
    dot.style.transform = 'translate3d(' + x1 + 'px,' + y1 + 'px,0)';
    stage.appendChild(dot);
    dot.animate([
      { transform: 'translate3d(' + x1 + 'px,' + y1 + 'px,0)', opacity: 0 },
      { transform: 'translate3d(' + ((x1 + x2) / 2) + 'px,'
                                  + ((y1 + y2) / 2 - 18) + 'px,0)', opacity: 1 },
      { transform: 'translate3d(' + x2 + 'px,' + y2 + 'px,0)', opacity: 0 }
    ], { duration: 1500, easing: 'cubic-bezier(.4,0,.3,1)' })
      .onfinish = function () { dot.remove(); };
  }

  function compare(before, after) {
    if (!before) return;
    for (var i = 0; i < ROOMS.length - 1; i++) {
      var here = ROOMS[i].key, next = ROOMS[i + 1].key;
      if (after[here] && before[here] && after[next] && before[next]
          && after[here].count < before[here].count
          && after[next].count > before[next].count) {
        runBetween(here, next);
      }
    }
  }

  function render(d) {
    var map = {};
    d.rooms.forEach(function (r) { map[r.key] = r; });
    var before = last;
    drawRooms(map); drawDesk(d.you); drawVault(d.vault);
    compare(before, map);
    last = map;
  }

  document.querySelector('.house').addEventListener('click', function (e) {
    var room = e.target.closest('.room.has');
    if (!room) return;
    var key = room.id.replace('room-', '');
    openRooms[key] = !openRooms[key];
    room.classList.toggle('open', openRooms[key]);
  });
  document.querySelector('.house').addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var room = e.target.closest('.room.has');
    if (!room) return;
    e.preventDefault();
    room.click();
  });

  function poll() {
    if (document.hidden) return;
    fetch('/house/data').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) render(d); })
      .catch(function () {});
  }
  render({{ initial|tojson }});     /* first paint sets the baseline, not a move */
  setInterval(poll, 4000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) poll();
  });
})();
</script>
{% endblock %}
"""


CALLS = """
{% extends "base" %}{% block body %}
<style>
.call{border:1px solid var(--line);border-radius:var(--r-md);padding:15px 17px;
  margin-bottom:12px;background:var(--panel-2)}
.call.cold{border-color:rgba(244,114,182,.26)}
.call .top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.call .top b{font-size:15px}
.call .where{font-size:12.5px;color:var(--mut);margin-top:2px}
.tag{font-size:9.5px;font-weight:700;letter-spacing:.1em;padding:3px 8px;
  border-radius:999px;text-transform:uppercase}
.tag.only{background:rgba(244,114,182,.16);color:#f9a8d4}
.tag.done{background:rgba(190,170,255,.08);color:var(--mut)}
.dial{display:flex;gap:9px;margin-top:12px;flex-wrap:wrap}
.dial a{font-size:15px;font-weight:650;letter-spacing:.01em}
.script{margin-top:12px;border-left:2px solid rgba(244,114,182,.35);
  padding:2px 0 2px 13px;font-size:13.5px;line-height:1.62;color:#cfc6e0;
  white-space:pre-wrap}
.after{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center;
  padding-top:13px;border-top:1px solid var(--line-2)}
.after input{width:auto;flex:1;min-width:190px}
.after form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
@media (max-width:800px){ .dial a{width:100%;text-align:center} }
</style>

<div class="card">
<div style="display:flex;align-items:baseline;gap:11px;flex-wrap:wrap">
  <h1 style="margin:0">Call list</h1>
  <span class="muted">{{ leads|length }} business{{ '' if leads|length == 1
    else 'es' }} you can phone today.</span>
</div>

<div class="note info" style="margin-top:14px">
  <div class="k">Why this is a page and not a robot</div>
  <p class="muted" style="margin:6px 0 0">Automatic cold texts and AI cold calls
  are a legal trap in the US — the rules follow the phone number, and most small
  business numbers are mobiles, at $500–$1,500 <b>per message or call</b>. So
  Solo Studio will never dial or text for you. <b>You</b> calling is completely
  normal business, it's free, and for trades it converts better than email ever
  will. The app finds them and lines them up; you press call.</p>
</div>

{% if not leads %}
  <p class="muted" style="margin-top:16px">Nobody to call yet. Businesses turn
  up here once <b>Scout</b> finds them and they have a phone number —
  Google Places gives you the number even when there's no email.</p>
{% endif %}

{% for item in leads %}
{% set l = item.lead %}
<div class="call {{ 'cold' if not l['email'] }}" style="margin-top:14px">
  <div class="top">
    <b>{{ l['name'] }}</b>
    {% if not l['email'] %}<span class="tag only">phone is the only way in</span>
    {% endif %}
    {% if l['last_called_at'] %}<span class="tag done">tried
      {{ l['last_called_at'][:10] }}</span>{% endif %}
  </div>
  <div class="where">{{ l['category'] or 'local business' }}{% if l['address'] %}
    · {{ l['address'] }}{% endif %}</div>

  <div class="dial">
    <a class="btn btn-primary" href="tel:{{ item.tel }}">📞 Call {{ l['phone'] }}</a>
    <a class="btn" href="sms:{{ item.tel }}">Text instead</a>
  </div>

  <details style="margin-top:12px">
    <summary class="muted" style="cursor:pointer;font-size:13px">
      What to say</summary>
    <div class="script">{{ item.opener }}</div>
  </details>

  <div class="after">
    <form method="post" action="{{ url_for('call_got_email', lead_id=l['id']) }}">
      <input type="email" name="email" required placeholder="Email they gave you">
      <button class="btn btn-primary">Got it — queue the email</button>
    </form>
    <form method="post" action="{{ url_for('call_logged', lead_id=l['id']) }}">
      <button class="btn">No answer</button></form>
    <form method="post" action="{{ url_for('call_pass', lead_id=l['id']) }}">
      <button class="btn btn-danger">Not interested</button></form>
  </div>
</div>
{% endfor %}
</div>
{% endblock %}
"""


SETUP_TEST = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Connection tests</h1>
<table><tbody>
{% for name, ok, detail in results %}
<tr><td><b>{{ name }}</b></td>
    <td>{% if ok %}<span style="color:var(--ok)">✓ working</span>
        {% else %}<span style="color:var(--bad)">✗ failed</span>{% endif %}</td>
    <td class="muted">{{ detail }}</td></tr>
{% endfor %}
</tbody></table>
<p><a class="btn" href="{{ url_for('setup') }}">Back to Setup</a></p>
</div>
{% endblock %}
"""


from jinja2 import DictLoader  # noqa: E402

app.jinja_env.loader = DictLoader({"base": BASE})


@app.template_filter("platform")
def _platform_name(url: str) -> str:
    """"facebook.com" out of a link."""
    return core.social_platform(url) or "social media"


@app.template_filter("reason")
def _site_reason(status: str) -> str:
    """The plain-English reason this business is worth pitching."""
    return core.SITE_REASON.get(status, status or "no website")


@app.context_processor
def _inject():
    try:
        pending = len(STATE.db.leads_awaiting_approval())
    except Exception:
        pending = 0
    try:
        stamp = _live_stamp()
    except Exception:
        stamp = ""
    try:
        callable_now = len(STATE.db.leads_to_call())
    except Exception:
        callable_now = 0
    return {"config": STATE.config, "pwa_meta": PWA_META,
            "cloud_mode": CLOUD_MODE, "pending_count": pending,
            "call_count": callable_now, "job": dict(JOB),
            "live_stamp": stamp, "update_ready": _restart_pending()}


def _render(tpl, **ctx):
    return render_template_string(tpl, **ctx)


JARVIS = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS — Solo Studio</title>
PWAMETA_PLACEHOLDER
<style>
:root{--cy:#a5b4fc;--cy2:#dbd9ff;--dim:#5c5384;--amber:#ff5e8a;--grn:#6ee7b7;
      --red:#ff5e8a;--ink:#efecff;--accent:#a5b4fc}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:radial-gradient(1200px 800px at 50% 42%,#171233 0%,#100c22 55%,#07050f 100%);
  color:var(--ink);font:14px/1.45 "SF Mono",Menlo,Consolas,monospace;overflow:hidden}
body.alert{--accent:var(--amber)}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:1;
  background-image:linear-gradient(rgba(165,180,252,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(165,180,252,.045) 1px,transparent 1px);
  background-size:44px 44px}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:2;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.12) 0 2px,transparent 2px 4px)}
/* a beam that sweeps down the whole display now and then */
#beam{position:fixed;left:0;right:0;height:180px;z-index:3;pointer-events:none;
  background:linear-gradient(180deg,transparent,rgba(165,180,252,.055),transparent);
  animation:sweep 9s linear infinite}
@keyframes sweep{0%{top:-180px}100%{top:100%}}
/* corner brackets framing the display */
.bracket{position:fixed;width:26px;height:26px;z-index:4;pointer-events:none;
  border-color:var(--accent);opacity:.55;transition:border-color .4s}
.bracket.tl{top:10px;left:10px;border-top:2px solid;border-left:2px solid}
.bracket.tr{top:10px;right:10px;border-top:2px solid;border-right:2px solid}
.bracket.bl{bottom:10px;left:10px;border-bottom:2px solid;border-left:2px solid}
.bracket.br{bottom:10px;right:10px;border-bottom:2px solid;border-right:2px solid}

.hud{position:relative;z-index:5;display:grid;height:100vh;padding:20px 30px;gap:12px;
  grid-template-rows:auto auto 1fr 196px;grid-template-columns:280px 1fr 300px;
  grid-template-areas:"top top top" "kpis kpis kpis" "left core right" "feed feed feed"}
.label{font-size:10px;letter-spacing:.22em;color:var(--dim);text-transform:uppercase}
.glow{text-shadow:0 0 14px rgba(165,180,252,.75),0 0 34px rgba(165,180,252,.30)}

/* ---- top bar ---- */
.top{grid-area:top;display:flex;align-items:center;gap:16px;
  border-bottom:1px solid rgba(165,180,252,.22);padding-bottom:12px}
.top .sys{font-size:17px;letter-spacing:.34em;color:var(--cy2)}
.top .greet{color:#8fc7e6;font-size:13px;letter-spacing:.06em;
  white-space:nowrap;overflow:hidden}
.top .greet::after{content:"▌";animation:blink 1s step-end infinite;color:var(--cy)}
.top .greet.done::after{content:""}
@keyframes blink{50%{opacity:0}}
.top .clock{margin-left:auto;font-size:17px;color:var(--cy2);letter-spacing:.18em}
.chip{font-size:10px;letter-spacing:.2em;padding:4px 12px;border:1px solid;border-radius:3px;
  white-space:nowrap}
.chip.on{color:var(--grn);border-color:rgba(74,222,128,.5);text-shadow:0 0 10px rgba(74,222,128,.7)}
.chip.off{color:var(--amber);border-color:rgba(255,180,84,.5);text-shadow:0 0 10px rgba(255,180,84,.6)}
/* the exit — always visible, big enough to tap */
.back{display:inline-flex;align-items:center;gap:7px;text-decoration:none;
  color:var(--cy2);font-size:12px;letter-spacing:.16em;padding:9px 16px;
  border:1px solid rgba(165,180,252,.45);border-radius:6px;background:rgba(165,180,252,.07);
  transition:.15s;white-space:nowrap}
.back:hover,.back:active{background:rgba(165,180,252,.2);border-color:var(--cy);
  box-shadow:0 0 18px rgba(165,180,252,.35)}

/* ---- kpi strip ---- */
.kpis{grid-area:kpis;display:flex;flex-wrap:wrap;gap:6px 30px;
  border-bottom:1px solid rgba(165,180,252,.14);padding:2px 0 10px}
.kpi .label{margin-bottom:1px}
.kpi b{font-size:21px;color:#fff;font-weight:600}
.kpi b.glow{text-shadow:0 0 10px rgba(165,180,252,.6)}

/* ---- money column ---- */
.left{grid-area:left;display:flex;flex-direction:column;justify-content:center;gap:22px}
.stat .label{margin-bottom:4px}
.stat b{display:block;font-size:37px;font-weight:600;color:#fff;line-height:1.05}
.stat .sub{font-size:11px;color:var(--dim);letter-spacing:.08em}

/* ---- reactor ---- */
.core{grid-area:core;display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:0}
.reactor{width:min(40vh,380px);height:min(40vh,380px);
  filter:drop-shadow(0 0 26px rgba(165,180,252,.35))}
.reactor circle,.reactor line,.reactor path{fill:none;stroke:var(--cy);
  vector-effect:non-scaling-stroke}
.rSeg{stroke-width:6;stroke-dasharray:58 30;opacity:.8;
  transform-origin:200px 200px;animation:spin 34s linear infinite}
.rSeg2{stroke-width:2.2;stroke-dasharray:6 12;opacity:.6;
  transform-origin:200px 200px;animation:spin 18s linear infinite reverse}
.rThin{stroke-width:1;opacity:.32}
.rSeg3{stroke-width:10;stroke-dasharray:22 46;opacity:.5;
  transform-origin:200px 200px;animation:spin 11s linear infinite}
.rIris{stroke-width:1.6;opacity:.5;stroke-dasharray:3 7;
  transform-origin:200px 200px;animation:spin 7s linear infinite reverse}
.spokes line{stroke-width:1.2;opacity:.4}
.spokes{transform-origin:200px 200px;animation:spin 74s linear infinite reverse}
.coreGlow{animation:pulse 3s ease-in-out infinite}
.coreRing{stroke-width:1.4;opacity:.75;stroke-dasharray:2 5;
  transform-origin:200px 200px;animation:spin 5s linear infinite}
#sweep{transform-origin:200px 200px;animation:spin 5s linear infinite}
.ticks line{stroke-width:1;opacity:.3}

/* the gauge — each arc is a stage of the pipeline */
#gaugeTrack path{stroke-width:15;opacity:.16;stroke-linecap:butt}
#gauge path{stroke-width:15;stroke-linecap:butt;cursor:default;
  transition:stroke-dashoffset 1.1s cubic-bezier(.22,.61,.36,1),opacity .6s}
#gauge path.lit{filter:drop-shadow(0 0 7px currentColor)}

/* motes riding the ring, one per deal in flight */
#orbit circle{fill:var(--cy2);stroke:none;opacity:.9;
  filter:drop-shadow(0 0 6px var(--cy))}
#orbit{transform-origin:200px 200px;animation:spin 22s linear infinite}

@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:.82;transform:scale(1)}
  50%{opacity:1;transform:scale(1.035)}}
.coreGlow{transform-origin:200px 200px}
body.alert .reactor{filter:drop-shadow(0 0 30px rgba(255,180,84,.45))}
body.alert .reactor circle,body.alert .reactor line{stroke:var(--amber)}
body.alert .coreGlow{animation:pulse 1.1s ease-in-out infinite}
.coreLabel{margin-top:16px;text-align:center}
.coreLabel .label{margin-bottom:5px}
.coreLabel b{font-size:15px;letter-spacing:.3em;color:var(--cy2)}
body.alert .coreLabel b{color:var(--amber);text-shadow:0 0 16px rgba(255,180,84,.6)}

/* ---- pipeline bars ---- */
/* "safe center" keeps the column centred while it fits and falls back to the
   top once the watch list makes it taller than the screen — plain centring
   clips the first item off the top, where the worst one lives. */
.right{grid-area:right;display:flex;flex-direction:column;
  justify-content:safe center;
  gap:12px;min-height:0;overflow-y:auto;scrollbar-width:thin}
.bar .label{display:flex;justify-content:space-between;margin-bottom:3px}
.bar .label span:last-child{color:var(--cy2)}
.track{height:7px;background:rgba(165,180,252,.10);border-radius:2px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,rgba(165,180,252,.35),var(--cy));
  box-shadow:0 0 10px rgba(165,180,252,.6);width:0;transition:width .9s ease}

/* ---- mission log ---- */
.feed{grid-area:feed;border-top:1px solid rgba(165,180,252,.22);padding-top:10px;
  overflow:hidden}
.feed .label{margin-bottom:8px}
/* What the watchman has found, worst first. Red is broken, amber is waiting
   on the owner, dim is worth knowing. */
#alerts{margin-bottom:14px}
#alerts .alert{display:flex;gap:9px;align-items:flex-start;padding:7px 0;
  border-bottom:1px solid rgba(150,170,255,.10);text-decoration:none;color:inherit}
#alerts .alert:last-child{border-bottom:0}
#alerts .pip{flex:0 0 auto;width:6px;height:6px;border-radius:50%;
  margin-top:5px;background:var(--dim)}
#alerts .alert.fix .pip{background:var(--red);
  box-shadow:0 0 0 3px rgba(255,122,94,.16)}
#alerts .alert.waiting .pip{background:var(--amber)}
#alerts .alert b{display:block;font-size:12.5px;font-weight:600;letter-spacing:.02em}
#alerts .alert span{font-size:11.5px;color:var(--dim);line-height:1.45}
#alerts .clear{font-size:12px;color:var(--dim);letter-spacing:.08em}
#feedlines{overflow:hidden;font-size:12.5px}
#feedlines div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  padding:1.5px 0;color:#c2bce8}
#feedlines div.fresh{animation:landed 1.6s ease-out}
@keyframes landed{0%{background:rgba(165,180,252,.22);transform:translateX(-6px)}
  100%{background:transparent;transform:none}}
#feedlines .t{color:var(--dim)}
#feedlines .k{color:var(--cy2)}
#feedlines .k.pay{color:var(--grn)} #feedlines .k.err{color:var(--red)}
#feedlines .k.warn{color:var(--amber)}

/* ---- ask console ---- */
#askbar{position:fixed;left:0;right:0;bottom:0;z-index:15;display:flex;
  align-items:center;gap:12px;padding:11px 22px;
  background:rgba(7,5,15,.985);backdrop-filter:blur(7px);
  border-top:1px solid rgba(165,180,252,.3)}
#askbar .caret{color:var(--cy);font-size:13px;letter-spacing:.2em;flex:0 0 auto}
#askbar input{flex:1;min-width:0;background:transparent;border:0;outline:0;
  color:var(--ink);font:inherit;font-size:14px;letter-spacing:.04em}
#askbar input::placeholder{color:var(--dim);letter-spacing:.14em}
#askbar .hint{color:var(--dim);font-size:10px;letter-spacing:.18em;flex:0 0 auto}
body.alert #askbar{border-top-color:rgba(255,94,138,.45)}

/* Starts below the top bar so the clock and the way out stay reachable while
   the console is open. Near-opaque — the HUD behind it must not compete with
   the text. */
#console{position:fixed;left:0;right:0;bottom:0;top:64px;z-index:14;
  display:none;flex-direction:column;padding:14px 22px 60px;
  background:rgba(7,5,15,.985);backdrop-filter:blur(7px);
  border-top:1px solid rgba(165,180,252,.16)}
#console.on{display:flex}
#console .chead{display:flex;align-items:center;gap:12px;flex:0 0 auto;
  border-bottom:1px solid rgba(165,180,252,.22);padding-bottom:9px;margin-bottom:12px}
#console .chead b{color:var(--cy2);font-size:12px;letter-spacing:.28em;font-weight:600}
#console .x{margin-left:auto;color:var(--dim);cursor:pointer;font-size:11px;
  letter-spacing:.18em;border:1px solid rgba(165,180,252,.3);border-radius:5px;
  padding:5px 11px;background:transparent;font-family:inherit}
#console .x:hover{color:var(--cy);border-color:var(--cy)}
#lines{flex:1;overflow-y:auto;font-size:13.5px;line-height:1.62;padding-right:6px}
#lines .turn{margin-bottom:15px;max-width:900px}
#lines .who{font-size:10px;letter-spacing:.2em;color:var(--dim);margin-bottom:3px}
#lines .you .who{color:var(--cy)}
#lines .body{white-space:pre-wrap;overflow-wrap:anywhere;color:#ded9f5}
#lines .you .body{color:var(--cy2)}
#lines .bad .body{color:var(--red)}
#lines .body::after{content:"";display:inline-block;width:7px;height:14px;
  vertical-align:-2px;margin-left:3px;background:var(--cy);opacity:0}
#lines .typing .body::after{opacity:1;animation:blink 1s step-end infinite}

/* ---- boot sequence ---- */
#boot{position:fixed;inset:0;z-index:20;background:#07050f;padding:9vh 8vw;
  font-size:13px;color:var(--cy);letter-spacing:.06em}
#boot div{opacity:0;animation:bootline .25s forwards}
@keyframes bootline{to{opacity:1}}
#boot .ok{color:var(--grn)}
#boot.gone{opacity:0;pointer-events:none;transition:opacity .5s}

@media (max-width:900px){
  body{overflow:auto}
  .hud{display:flex;flex-direction:column;height:auto;gap:18px;padding:16px 18px 30px}
  .top{flex-wrap:wrap;row-gap:10px}
  .top .sys{font-size:15px;letter-spacing:.26em}
  .top .clock{margin-left:auto;font-size:15px}
  .top .greet{order:9;flex-basis:100%;white-space:normal}
  .back{order:-1}                     /* the exit comes first on a phone */
  .hud{padding-bottom:70px}           /* clear the ask bar pinned at the bottom */
  #askbar{padding:10px 14px;gap:8px}
  #askbar .hint{display:none}
  #askbar input{font-size:16px}       /* 16px stops iOS zooming on focus */
  #console{padding:16px 14px 62px;bottom:0;top:0}
  #console .chead b{font-size:11px;letter-spacing:.18em}
  #console #cstatus{display:none}   /* no room for it next to the close button */
  #console .x{white-space:nowrap;padding:7px 12px}
  .bracket{display:none}
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px 10px;padding-bottom:14px}
  /* two lines reserved for every label, so wrapped ones don't shove
     their number out of line with the rest of the row */
  .kpi{min-width:0;display:flex;flex-direction:column;justify-content:flex-end}
  .kpi .label{font-size:9px;letter-spacing:.13em;overflow-wrap:anywhere;
    min-height:2.4em;display:flex;align-items:flex-end}
  .kpi b{font-size:19px}
  .left{display:grid;grid-template-columns:1fr 1fr;gap:18px 20px}
  .stat b{font-size:31px}
  .reactor{width:230px;height:230px}
  .right{gap:10px}
  .feed{padding-bottom:10px}
  #feedlines div{white-space:normal}
}
@media (prefers-reduced-motion:reduce){
  #beam,.rSeg,.rSeg2,.rSeg3,.spokes,#sweep,.coreGlow{animation:none}
}
</style></head><body>
<div id="boot"></div>
<div id="beam"></div>
<div class="bracket tl"></div><div class="bracket tr"></div>
<div class="bracket bl"></div><div class="bracket br"></div>
<div class="hud">
  <div class="top">
    <span class="sys glow">J.A.R.V.I.S</span>
    <a class="back" href="/">◀ EXIT TO DASHBOARD</a>
    <span class="chip" id="autopilot">…</span>
    <span class="clock glow" id="clock">--:--:--</span>
    <span class="greet" id="greet"></span>
  </div>
  <div class="kpis" id="kpis"></div>
  <div class="left" id="money"></div>
  <div class="core">
    <svg class="reactor" viewBox="0 0 400 400" aria-hidden="true">
      <defs>
        <radialGradient id="cg" cx="50%" cy="50%">
          <stop offset="0%" stop-color="#f4f2ff" stop-opacity="1"/>
          <stop offset="34%" stop-color="#dbd9ff" stop-opacity=".95"/>
          <stop offset="70%" stop-color="#7c6ad8" stop-opacity=".55"/>
          <stop offset="100%" stop-color="#2a1f5c" stop-opacity="0"/>
        </radialGradient>
        <linearGradient id="sw" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stop-color="#a5b4fc" stop-opacity="0"/>
          <stop offset="100%" stop-color="#a5b4fc" stop-opacity=".55"/>
        </linearGradient>
      </defs>
      <circle class="rSeg"  cx="200" cy="200" r="190"/>
      <circle class="rThin" cx="200" cy="200" r="176"/>
      <g class="ticks" id="ticks"></g>
      <!-- the gauge: one arc per pipeline stage, lit by how many are in it -->
      <g id="gaugeTrack"></g>
      <g id="gauge"></g>
      <circle class="rThin" cx="200" cy="200" r="146"/>
      <circle class="rSeg2" cx="200" cy="200" r="136"/>
      <g id="sweep"><path d="M200 200 L200 68 A132 132 0 0 1 293 107 Z"
        fill="url(#sw)" stroke="none"/></g>
      <g class="spokes" id="spokes"></g>
      <g id="orbit"></g>
      <circle class="rThin" cx="200" cy="200" r="100"/>
      <circle class="rSeg3" cx="200" cy="200" r="80"/>
      <circle class="rIris" cx="200" cy="200" r="62"/>
      <circle class="coreGlow" cx="200" cy="200" r="50" fill="url(#cg)" stroke="none"/>
      <circle class="coreRing" cx="200" cy="200" r="34"/>
    </svg>
    <div class="coreLabel">
      <div class="label">Outer ring · your pipeline by stage</div>
      <b class="glow" id="coreState">SYSTEMS NOMINAL</b>
    </div>
  </div>
  <div class="right">
    <div id="alerts"></div>
    <div id="bars"></div>
  </div>
  <div class="feed">
    <div class="label">Mission log — live</div>
    <div id="feedlines"></div>
  </div>
</div>
<div id="console" aria-live="polite">
  <div class="chead">
    <b>J.A.R.V.I.S CONSOLE</b>
    <span class="label" id="cstatus">standing by</span>
    <button class="x" id="cclose" type="button">CLOSE  ESC</button>
  </div>
  <div id="lines"></div>
</div>

<form id="askbar" autocomplete="off">
  <span class="caret">▸</span>
  <input id="askq" placeholder="ASK JARVIS ANYTHING…" autocomplete="off"
         spellcheck="false" aria-label="Ask JARVIS">
  <span class="hint">ENTER TO SEND</span>
</form>

<script>
(function(){
  /* ---- reactor furniture ---- */
  var spokes = document.getElementById('spokes');
  for (var i = 0; i < 12; i++) {
    var a = i * Math.PI / 6;
    var l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l.setAttribute('x1', 200 + 108 * Math.cos(a)); l.setAttribute('y1', 200 + 108 * Math.sin(a));
    l.setAttribute('x2', 200 + 146 * Math.cos(a)); l.setAttribute('y2', 200 + 146 * Math.sin(a));
    spokes.appendChild(l);
  }
  /* The gauge: eight arcs round the core, one per stage of the pipeline, in
     the order work moves through it. Each fills with how many leads are
     sitting there — so the ring is a readout, not decoration. */
  var STAGES = [
    { key: 'found',             c: '#f9a8d4' },
    { key: 'contacted',         c: '#a5b4fc' },
    { key: 'building_preview',  c: '#d8b4fe' },
    { key: 'preview_sent',      c: '#fde68a' },
    { key: 'sending_payment_link', c: '#d8b4fe' },
    { key: 'payment_link_sent', c: '#8fe6f5' },
    { key: 'paid',              c: '#8ff0cb' },
    { key: 'delivered',         c: '#6ee7b7' }
  ];
  var GR = 160, GAP = 3.2, SEG = 360 / STAGES.length;

  function arcPath(from, to) {
    var a = (from - 90) * Math.PI / 180, b = (to - 90) * Math.PI / 180;
    return 'M ' + (200 + GR * Math.cos(a)).toFixed(2) + ' '
                + (200 + GR * Math.sin(a)).toFixed(2)
         + ' A ' + GR + ' ' + GR + ' 0 ' + (to - from > 180 ? 1 : 0) + ' 1 '
                + (200 + GR * Math.cos(b)).toFixed(2) + ' '
                + (200 + GR * Math.sin(b)).toFixed(2);
  }
  var gTrack = document.getElementById('gaugeTrack'),
      gLive = document.getElementById('gauge'), gPaths = [];
  STAGES.forEach(function (st, i) {
    var d = arcPath(i * SEG + GAP, (i + 1) * SEG - GAP),
        len = GR * (SEG - GAP * 2) * Math.PI / 180;
    var bg = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    bg.setAttribute('d', d); gTrack.appendChild(bg);
    var fg = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    fg.setAttribute('d', d);
    fg.setAttribute('stroke', st.c);
    fg.style.color = st.c;                 /* for the drop-shadow */
    fg.style.strokeDasharray = len.toFixed(2);
    fg.style.strokeDashoffset = len.toFixed(2);
    var tip = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    fg.appendChild(tip);
    gLive.appendChild(fg);
    gPaths.push({ el: fg, len: len, tip: tip,
                  label: st.key.replace(/_/g, ' ') });
  });

  /* one mote orbiting for each deal actually in flight */
  var orbit = document.getElementById('orbit');
  function setOrbit(n) {
    n = Math.max(0, Math.min(14, n));
    while (orbit.childNodes.length > n) orbit.removeChild(orbit.lastChild);
    while (orbit.childNodes.length < n) {
      var i = orbit.childNodes.length, a = (i / 14) * Math.PI * 2;
      var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', (200 + 176 * Math.cos(a)).toFixed(1));
      c.setAttribute('cy', (200 + 176 * Math.sin(a)).toFixed(1));
      c.setAttribute('r', 2.6);
      orbit.appendChild(c);
    }
  }

  var ticks = document.getElementById('ticks');
  for (var t = 0; t < 60; t++) {
    var ta = t * Math.PI / 30, len = (t % 5 === 0) ? 12 : 6;
    var tl = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    tl.setAttribute('x1', 200 + 168 * Math.cos(ta)); tl.setAttribute('y1', 200 + 168 * Math.sin(ta));
    tl.setAttribute('x2', 200 + (168 - len) * Math.cos(ta));
    tl.setAttribute('y2', 200 + (168 - len) * Math.sin(ta));
    ticks.appendChild(tl);
  }

  /* ---- clock ---- */
  function pad(n){ return (n < 10 ? '0' : '') + n; }
  function tickClock(){
    var d = new Date();
    document.getElementById('clock').textContent =
      pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  setInterval(tickClock, 1000); tickClock();

  /* ---- boot sequence, once ---- */
  var BOOT = ['SOLO STUDIO CORE v2 … ONLINE',
              'LEAD SCOUT … STANDING BY',
              'RESEARCHER … STANDING BY',
              'TRIAGE + DESIGNER … STANDING BY',
              'PAYMENT GATE … ARMED',
              'ALL SYSTEMS LINKED'];
  var boot = document.getElementById('boot');
  BOOT.forEach(function (line, i) {
    var el = document.createElement('div');
    el.style.animationDelay = (i * 0.13) + 's';
    el.innerHTML = '&gt; ' + line.replace(/(ONLINE|STANDING BY|ARMED|LINKED)/,
                                          '<span class="ok">$1</span>');
    boot.appendChild(el);
  });
  setTimeout(function(){ boot.classList.add('gone'); }, 1250);
  setTimeout(function(){ boot.remove(); }, 1800);

  /* ---- typed greeting ---- */
  function typeOut(el, text){
    if (el.dataset.typed === text) return;
    el.dataset.typed = text; el.textContent = ''; el.classList.remove('done');
    var i = 0;
    (function step(){
      el.textContent = text.slice(0, ++i);
      if (i < text.length) setTimeout(step, 16); else el.classList.add('done');
    })();
  }

  /* ---- numbers that count up to their new value ---- */
  var shown = {};
  function setNumber(el, key, value){
    var target = parseFloat(String(value).replace(/[^0-9.\\-]/g, ''));
    if (isNaN(target)) { el.textContent = value; return; }
    var prefix = /^\\$/.test(String(value)) ? '$' : '';
    var suffix = /%$/.test(String(value)) ? '%' : '';
    var from = shown[key] === undefined ? target : shown[key];
    shown[key] = target;
    if (from === target) { el.textContent = value; return; }
    var start = performance.now(), dur = 650;
    (function frame(now){
      var p = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      var v = Math.round(from + (target - from) * eased);
      el.textContent = prefix + v.toLocaleString() + suffix;
      if (p < 1) requestAnimationFrame(frame); else el.textContent = value;
    })(start);
  }

  var STAGE_LABELS = {found:'FOUND', contacted:'CONTACTED', building_preview:'BUILDING',
    preview_sent:'PREVIEW SENT', sending_payment_link:'SENDING LINK',
    payment_link_sent:'AWAITING PAYMENT', paid:'PAID', deploying_final:'DEPLOYING',
    delivered:'DELIVERED', not_interested:'PASSED', error:'ATTENTION'};
  var lastTopEvent = null;

  function render(d){
    var greetWord = 'Good evening'; var h = new Date().getHours();
    if (h >= 5 && h < 12) greetWord = 'Good morning';
    else if (h >= 12 && h < 18) greetWord = 'Good afternoon';
    var faults = d.findings.filter(function (f) { return f.level === 'fix'; }).length,
        pending = d.findings.length - faults,
        report;
    if (faults) {
      report = faults === 1 ? 'One thing needs fixing.'
                            : faults + ' things need fixing.';
    } else if (pending) {
      report = pending === 1 ? 'One thing is waiting on you.'
                             : pending + ' things are waiting on you.';
    } else {
      report = 'All services standing by.';
    }
    typeOut(document.getElementById('greet'),
            greetWord + (d.owner ? ', ' + d.owner : '') + '. ' + report);

    var ap = document.getElementById('autopilot');
    ap.textContent = d.autopilot ? 'AUTOPILOT · ONLINE' : 'AUTOPILOT · STANDBY';
    ap.className = 'chip ' + (d.autopilot ? 'on' : 'off');

    document.body.classList.toggle('alert', d.attention > 0);

    /* Drive the gauge: each arc fills against the busiest stage, so the ring
       shows the shape of the pipeline rather than raw totals. */
    var peak = 1;
    STAGES.forEach(function (st) { peak = Math.max(peak, d.stages[st.key] || 0); });
    STAGES.forEach(function (st, i) {
      var n = d.stages[st.key] || 0, g = gPaths[i];
      g.el.style.strokeDashoffset = (g.len * (1 - n / peak)).toFixed(2);
      g.el.style.opacity = n ? 1 : 0.22;
      g.el.classList.toggle('lit', n > 0);
      g.tip.textContent = g.label + ' — ' + n;
    });
    setOrbit((d.kpis && d.stages) ? (d.stages.contacted || 0)
      + (d.stages.preview_sent || 0) + (d.stages.payment_link_sent || 0)
      + (d.stages.paid || 0) : 0);

    var money = document.getElementById('money');
    if (money.children.length !== d.money.length) money.textContent = '';
    d.money.forEach(function (m, i) {
      var w = money.children[i];
      if (!w) {
        w = document.createElement('div'); w.className = 'stat';
        w.innerHTML = '<div class="label"></div><b class="glow"></b><div class="sub"></div>';
        money.appendChild(w);
      }
      w.children[0].textContent = m.l;
      setNumber(w.children[1], 'money' + i, m.v);
      w.children[2].textContent = m.s || '';
    });

    var kpis = document.getElementById('kpis');
    if (kpis.children.length !== d.kpis.length) kpis.textContent = '';
    d.kpis.forEach(function (m, i) {
      var w = kpis.children[i];
      if (!w) {
        w = document.createElement('div'); w.className = 'kpi';
        w.innerHTML = '<div class="label"></div><b></b>';
        kpis.appendChild(w);
      }
      w.children[0].textContent = m.l;
      w.children[1].className = m.hot ? 'glow' : '';
      setNumber(w.children[1], 'kpi' + i, m.v);
    });

    var alerts = document.getElementById('alerts');
    alerts.textContent = '';
    var lab = document.createElement('div');
    lab.className = 'label';
    lab.textContent = 'What needs you';
    alerts.appendChild(lab);
    if (!d.findings.length) {
      var ok = document.createElement('div');
      ok.className = 'clear'; ok.textContent = 'Nothing. All clear.';
      alerts.appendChild(ok);
    }
    d.findings.forEach(function (f) {
      var a = document.createElement('a');
      a.className = 'alert ' + f.level; a.href = f.where;
      var pip = document.createElement('span'); pip.className = 'pip';
      var box = document.createElement('div');
      var b = document.createElement('b'); b.textContent = f.title;
      var sp = document.createElement('span'); sp.textContent = f.detail;
      box.appendChild(b); box.appendChild(sp);
      a.appendChild(pip); a.appendChild(box);
      alerts.appendChild(a);
    });

    var broken = d.findings.filter(function (f) { return f.level === 'fix'; }).length;
    document.getElementById('coreState').textContent =
      broken ? broken + (broken === 1 ? ' FAULT' : ' FAULTS') + ' — NEEDS YOU'
             : (d.findings.length ? d.findings.length + ' WAITING ON YOU'
                                  : 'SYSTEMS NOMINAL');

    var bars = document.getElementById('bars'); bars.textContent = '';
    var max = 1, k;
    for (k in d.stages) if (d.stages[k] > max) max = d.stages[k];
    for (k in d.stages) {
      var wrap = document.createElement('div'); wrap.className = 'bar';
      var lab = document.createElement('div'); lab.className = 'label';
      var s1 = document.createElement('span'); s1.textContent = STAGE_LABELS[k] || k;
      var s2 = document.createElement('span'); s2.textContent = d.stages[k];
      lab.appendChild(s1); lab.appendChild(s2);
      var track = document.createElement('div'); track.className = 'track';
      var fill = document.createElement('div'); fill.className = 'fill';
      track.appendChild(fill); wrap.appendChild(lab); wrap.appendChild(track);
      bars.appendChild(wrap);
      (function(f, w2){ requestAnimationFrame(function(){ f.style.width = w2 + '%'; }); })
        (fill, Math.round(100 * d.stages[k] / max));
    }

    var feed = document.getElementById('feedlines'); feed.textContent = '';
    if (!d.events.length) {
      var e0 = document.createElement('div');
      e0.textContent = '[--:--:--] awaiting first mission — find leads from the dashboard';
      feed.appendChild(e0);
    }
    var newestKey = d.events.length ? d.events[0].time + d.events[0].detail : null;
    var isNew = newestKey && lastTopEvent !== null && newestKey !== lastTopEvent;
    d.events.forEach(function(ev, idx){
      var line = document.createElement('div');
      if (isNew && idx === 0) line.className = 'fresh';
      var t = document.createElement('span'); t.className = 't';
      t.textContent = '[' + ev.time + '] ';
      var kEl = document.createElement('span');
      kEl.className = 'k' + (ev.kind.indexOf('pay') === 0 || ev.kind === 'delivered' ? ' pay'
        : ev.kind.indexOf('error') >= 0 || ev.kind.indexOf('fail') >= 0 ? ' err'
        : ev.kind.indexOf('attention') >= 0 || ev.kind.indexOf('unmatched') >= 0 ? ' warn' : '');
      kEl.textContent = ev.kind.toUpperCase().replace(/_/g, ' ');
      var dEl = document.createElement('span'); dEl.textContent = '  ' + ev.detail;
      line.appendChild(t); line.appendChild(kEl); line.appendChild(dEl);
      feed.appendChild(line);
    });
    lastTopEvent = newestKey;
  }

  function refresh(){
    fetch('/jarvis/data').then(function(r){ return r.json(); }).then(render)
      .catch(function(){ document.getElementById('coreState').textContent = 'LINK LOST — RETRYING'; });
  }
  setInterval(refresh, 4000); refresh();

  /* ---- ask console -------------------------------------------------------
     Same endpoints as the Ask page, so one conversation follows you between
     the two screens. Advisory only — there is nothing here that can act. */
  var panel = document.getElementById('console'),
      lines = document.getElementById('lines'),
      status = document.getElementById('cstatus'),
      bar = document.getElementById('askbar'),
      input = document.getElementById('askq'),
      busy = false, loaded = false;

  function turn(who, text, cls){
    var wrap = document.createElement('div');
    wrap.className = 'turn ' + (cls || '');
    var w = document.createElement('div'); w.className = 'who'; w.textContent = who;
    var b = document.createElement('div'); b.className = 'body'; b.textContent = text;
    wrap.appendChild(w); wrap.appendChild(b);
    lines.appendChild(wrap);
    lines.scrollTop = lines.scrollHeight;
    return wrap;
  }

  /* Reveal an answer the way a terminal would, but never make them wait:
     long replies print several characters a tick so it always lands ~1s. */
  function typeInto(wrap, text){
    var body = wrap.querySelector('.body'), i = 0,
        step = Math.max(1, Math.ceil(text.length / 90));
    wrap.classList.add('typing');
    body.textContent = '';
    var timer = setInterval(function(){
      i += step;
      body.textContent = text.slice(0, i);
      lines.scrollTop = lines.scrollHeight;
      if (i >= text.length) { clearInterval(timer); wrap.classList.remove('typing'); }
    }, 11);
  }

  function open(){ panel.classList.add('on'); lines.scrollTop = lines.scrollHeight; }
  function close(){ panel.classList.remove('on'); }

  function loadHistory(){
    if (loaded) return Promise.resolve();
    loaded = true;
    return fetch('/ask/history').then(function(r){ return r.json(); })
      .then(function(d){
        (d.messages || []).forEach(function(m){
          turn(m.role === 'user' ? 'YOU' : 'JARVIS', m.content,
               m.role === 'user' ? 'you' : '');
        });
        if (!d.has_key) {
          turn('JARVIS', 'Add your Anthropic (Claude) key on the Setup page and '
             + 'I can start answering.', 'bad');
        }
      }).catch(function(){ loaded = false; });
  }

  function ask(text){
    text = (text || '').trim();
    if (!text || busy) return;
    busy = true;
    input.value = '';
    open();
    loadHistory().then(function(){
      turn('YOU', text, 'you');
      var pending = turn('JARVIS', 'thinking', 'typing');
      status.textContent = 'processing';
      fetch('/ask/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text})
      })
      .then(function(r){ return r.json(); })
      .then(function(d){
        pending.classList.remove('typing');
        if (d.ok) { typeInto(pending, d.reply); }
        else { pending.className = 'turn bad';
               pending.querySelector('.body').textContent =
                 d.error || 'Something went wrong.'; }
      })
      .catch(function(){
        pending.className = 'turn bad';
        pending.querySelector('.body').textContent =
          'Link lost — check your connection and ask again.';
      })
      .finally(function(){
        busy = false;
        status.textContent = 'standing by';
        input.focus();
      });
    });
  }

  bar.addEventListener('submit', function(e){ e.preventDefault(); ask(input.value); });
  input.addEventListener('focus', function(){ open(); loadHistory(); });
  document.getElementById('cclose').addEventListener('click', function(){
    close(); input.blur();
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape') { close(); input.blur(); return; }
    // Start typing anywhere on the HUD and the question box takes it.
    if (document.activeElement !== input && e.key.length === 1
        && !e.metaKey && !e.ctrlKey && !e.altKey) {
      input.focus();
    }
  });
})();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    # `code` lets the launcher notice that a different version is already
    # running, instead of silently handing the user the old one.
    return {"app": HEALTH_MARKER, "code": core.code_fingerprint()}


def _live_snapshot() -> dict:
    """What the pages poll. The first three drive the refresh pill (see
    _live_stamp) — which is also what brings the page back when JARVIS finishes
    a hunt; the rest are read by the JARVIS screen."""
    db = STATE.db
    latest = db.recent_events(1)
    leads = db.all_leads()
    active = sum(1 for l in leads if l["stage"] in (
        core.STAGE_CONTACTED, core.STAGE_BUILDING_PREVIEW, core.STAGE_PREVIEW_SENT,
        core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT,
        core.STAGE_PAID, core.STAGE_DEPLOYING_FINAL))
    return {
        "pending": len(db.leads_awaiting_approval()),
        "attention": len(db.attention_events()),
        "last_event": latest[0]["id"] if latest else 0,
        "leads": len(leads),
        "active": active,
        "paid": sum(1 for l in leads if l["paid_at"]),
        "revenue": db.revenue_cents() // 100,
    }


def _live_stamp(snap: dict | None = None) -> str:
    s = snap or _live_snapshot()
    return f"{s['last_event']}:{s['pending']}:{s['attention']}"


@app.get("/live")
def live():
    """Tiny snapshot the ordinary pages poll so they stay current."""
    return _live_snapshot()


@app.get("/manifest.webmanifest")
def manifest():
    return {
        "name": "Solo Studio", "short_name": "Solo Studio",
        "start_url": "/", "display": "standalone",
        "background_color": "#0b0912", "theme_color": "#0b0912",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, 200, {"Content-Type": "application/manifest+json"}


@app.get("/favicon.ico")
def favicon():
    return ICON_192, 200, {"Content-Type": "image/png",
                           "Cache-Control": "public, max-age=86400"}


@app.get("/icon-<int:size>.png")
def icon_png(size):
    data = ICON_512 if size >= 512 else ICON_192
    return data, 200, {"Content-Type": "image/png",
                       "Cache-Control": "public, max-age=86400"}


PIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solo Studio</title>{{ pwa_meta|safe }}
<style>body{background:#0b0912;color:#f0ecf7;font:16px -apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{text-align:center;padding:24px}
h1{font-size:20px;letter-spacing:.3em;color:#dbd9ff;font-weight:600}
input{font-size:28px;text-align:center;letter-spacing:.4em;width:220px;padding:12px;
border-radius:10px;border:1px solid #3d3468;background:#1a1533;color:#f0ecf7;margin:18px 0}
button{font-size:16px;font-weight:600;padding:12px 40px;border-radius:10px;border:0;
background:#f472b6;color:#26071a}
.err{color:#ffab94;min-height:1.4em}</style></head>
<body><form method="post">
<h1>SOLO STUDIO</h1>
<div class="err">{{ error or "" }}</div>
<input type="password" name="pin" inputmode="numeric" autocomplete="one-time-code"
  placeholder="PIN" autofocus>
<div><button>Unlock</button></div>
</form></body></html>"""


@app.route("/pin", methods=["GET", "POST"])
def pin():
    error = None
    if request.method == "POST":
        want = str(STATE.config.get("phone_pin") or "")
        got = (request.form.get("pin") or "").strip()
        if want and hmac.compare_digest(got, want):
            session["phone_ok"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        time.sleep(1)  # slow down guessing
        error = "Wrong PIN — try again."
    return render_template_string(PIN_PAGE, error=error, pwa_meta=PWA_META)


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solo Studio</title>{{ pwa_meta|safe }}
<style>body{background:#0b0912;color:#f0ecf7;font:16px -apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{text-align:center;padding:24px;width:100%;max-width:340px}
h1{font-size:20px;letter-spacing:.3em;color:#dbd9ff;font-weight:600}
input{font-size:18px;width:100%;padding:14px;border-radius:10px;
border:1px solid #3d3468;background:#1a1533;color:#f0ecf7;margin:18px 0}
button{font-size:16px;font-weight:600;padding:14px 40px;border-radius:10px;
border:0;background:#f472b6;color:#26071a;width:100%}
.err{color:#ffab94;min-height:1.4em;font-size:14px}</style></head>
<body><form method="post">
<h1>SOLO STUDIO</h1>
<div class="err">{{ error or "" }}</div>
<input type="password" name="password" placeholder="Password" autofocus
  autocomplete="current-password">
<div><button>Sign in</button></div>
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not CLOUD_MODE:
        return redirect(url_for("dashboard"))
    if _password_too_weak():
        return render_template_string(
            LOGIN_PAGE, pwa_meta=PWA_META,
            error=(f"Set a longer password ({MIN_CLOUD_PASSWORD}+ characters) in "
                   "your host's SOLO_STUDIO_PASSWORD setting, then redeploy. "
                   "Nobody can sign in until you do.")), 503
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
        ip = ip.split(",")[0].strip()
        wait = _throttled(ip)
        if wait:
            error = f"Too many attempts. Try again in {wait // 60 + 1} minutes."
        elif hmac.compare_digest(request.form.get("password", ""), CLOUD_PASSWORD):
            _clear_failures(ip)
            session["cloud_ok"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            _record_failure(ip)
            time.sleep(1)
            error = "Wrong password."
    return render_template_string(LOGIN_PAGE, error=error, pwa_meta=PWA_META)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login" if CLOUD_MODE else "dashboard"))


# The continental United States: state outlines from the US Census boundary
# data (public domain), simplified to 2,397 points and embedded rather than
# fetched, so the map draws whether or not the machine is online.
#
# The bounds are the outline's own, so the dots and the coastline are drawn by
# the same projection and cannot drift apart. Alaska and Hawaii are outside it
# — the map says how many dots fall off rather than stretching the country to
# fit them in.
MAP_W, MAP_E = -124.71, -66.98
MAP_N, MAP_S = 49.38, 25.12
# Longitude degrees are shorter than latitude ones away from the equator;
# without this the country comes out visibly stretched sideways.
MAP_ASPECT = 0.79        # cos(37.5°), the middle of the country

US_OUTLINE = r"""[[[-87.36,35.0,-85.61,34.98,-85.43,34.12,-85.18,32.86,-85.07,32.58,-84.96,32.42,-85.0,32.32,-84.89,32.26,-85.06,32.14,-85.05,32.01,-85.14,31.84,-85.04,31.54,-85.11,31.28,-85.0,31.0,-85.5,31.0,-87.6,31.0,-87.63,30.87,-87.41,30.67,-87.45,30.51,-87.37,30.43,-87.52,30.28,-87.66,30.25,-87.91,30.41,-87.93,30.66,-88.01,30.69,-88.1,30.5,-88.14,30.32,-88.39,30.37,-88.47,31.9,-88.24,33.8,-88.1,34.89,-88.2,35.0,-87.36,35.0]],[[-109.04,37.0,-109.05,31.33,-111.07,31.33,-112.25,31.7,-114.82,32.49,-114.72,32.72,-114.52,32.76,-114.47,32.84,-114.52,33.03,-114.66,33.03,-114.73,33.41,-114.52,33.55,-114.5,33.7,-114.54,33.93,-114.42,34.11,-114.26,34.17,-114.14,34.31,-114.33,34.45,-114.47,34.71,-114.63,34.88,-114.63,35.0,-114.57,35.14,-114.6,35.32,-114.68,35.52,-114.74,36.1,-114.37,36.14,-114.25,36.02,-114.15,36.03,-114.05,36.2,-114.05,37.0,-110.5,37.01,-109.04,37.0]],[[-94.47,36.5,-90.15,36.5,-90.06,36.3,-90.22,36.18,-90.38,36.0,-89.73,36.0,-89.76,35.81,-89.91,35.76,-89.94,35.6,-90.13,35.44,-90.11,35.2,-90.21,35.02,-90.31,35.0,-90.25,34.91,-90.41,34.83,-90.48,34.66,-90.59,34.62,-90.57,34.42,-90.75,34.37,-90.74,34.3,-90.95,34.14,-90.89,34.03,-91.07,33.87,-91.23,33.56,-91.06,33.43,-91.14,33.35,-91.09,33.14,-91.17,33.0,-93.61,33.02,-94.04,33.02,-94.04,33.55,-94.18,33.59,-94.38,33.54,-94.48,33.64,-94.43,35.4,-94.62,36.5,-94.47,36.5]],[[-123.23,42.01,-122.38,42.01,-121.04,42.0,-120.0,42.0,-120.0,40.26,-120.0,39.0,-118.71,38.1,-117.5,37.22,-116.54,36.5,-115.85,35.97,-114.63,35.0,-114.63,34.88,-114.47,34.71,-114.33,34.45,-114.14,34.31,-114.26,34.17,-114.42,34.11,-114.54,33.93,-114.5,33.7,-114.52,33.55,-114.73,33.41,-114.66,33.03,-114.52,33.03,-114.47,32.84,-114.52,32.76,-114.72,32.72,-116.05,32.62,-117.13,32.54,-117.25,32.67,-117.25,32.88,-117.33,33.12,-117.47,33.3,-117.78,33.54,-118.18,33.76,-118.26,33.7,-118.41,33.74,-118.39,33.84,-118.57,34.04,-118.8,34.0,-119.22,34.15,-119.28,34.27,-119.56,34.42,-119.88,34.41,-120.14,34.48,-120.47,34.45,-120.65,34.58,-120.61,34.86,-120.67,34.9,-120.63,35.1,-120.89,35.25,-120.91,35.45,-121.0,35.46,-121.17,35.64,-121.28,35.67,-121.33,35.78,-121.72,36.2,-121.9,36.32,-121.94,36.64,-121.86,36.61,-121.79,36.8,-121.93,36.98,-122.11,36.96,-122.34,37.12,-122.42,37.24,-122.4,37.36,-122.52,37.52,-122.52,37.78,-122.33,37.78,-122.41,38.15,-122.49,38.11,-122.5,37.93,-122.7,37.89,-122.94,38.03,-122.98,38.27,-123.13,38.45,-123.33,38.57,-123.44,38.7,-123.74,38.96,-123.69,39.03,-123.82,39.37,-123.76,39.55,-123.85,39.83,-124.11,40.11,-124.36,40.26,-124.41,40.44,-124.16,40.88,-124.11,41.03,-124.16,41.14,-124.07,41.44,-124.15,41.72,-124.26,41.78,-124.21,42.0,-123.23,42.01]],[[-107.92,41.0,-105.73,41.0,-104.05,41.0,-102.05,41.0,-102.05,40.0,-102.04,36.99,-103.0,37.0,-104.34,36.99,-106.87,36.99,-107.42,37.0,-109.04,37.0,-109.04,38.17,-109.06,38.28,-109.05,39.13,-109.05,41.0,-107.92,41.0]],[[-73.05,42.04,-71.8,42.02,-71.8,42.01,-71.8,41.41,-71.86,41.32,-71.95,41.34,-72.39,41.26,-72.91,41.28,-73.13,41.15,-73.37,41.1,-73.66,40.99,-73.73,41.1,-73.48,41.21,-73.55,41.29,-73.49,42.05,-73.05,42.04]],[[-75.41,39.8,-75.51,39.68,-75.61,39.62,-75.59,39.46,-75.44,39.31,-75.4,39.07,-75.19,38.81,-75.09,38.8,-75.05,38.45,-75.69,38.46,-75.79,39.72,-75.62,39.83,-75.41,39.8]],[[-77.04,38.99,-76.91,38.9,-77.04,38.79,-77.12,38.93,-77.04,38.99]],[[-85.5,31.0,-85.0,31.0,-84.87,30.71,-83.5,30.65,-82.22,30.57,-82.17,30.36,-82.05,30.36,-82.0,30.56,-82.04,30.75,-81.95,30.83,-81.72,30.75,-81.44,30.71,-81.38,30.27,-81.26,29.79,-80.97,29.15,-80.52,28.46,-80.59,28.41,-80.57,28.09,-80.38,27.74,-80.09,27.02,-80.03,26.8,-80.04,26.57,-80.15,25.74,-80.24,25.72,-80.34,25.47,-80.3,25.38,-80.5,25.2,-80.57,25.24,-80.76,25.16,-81.08,25.12,-81.17,25.22,-81.13,25.38,-81.35,25.82,-81.53,25.9,-81.68,25.84,-81.8,26.09,-81.83,26.29,-82.04,26.52,-82.09,26.67,-82.06,26.88,-82.17,26.92,-82.15,26.79,-82.25,26.76,-82.57,27.3,-82.69,27.44,-82.39,27.84,-82.59,27.82,-82.72,27.69,-82.85,27.89,-82.68,28.43,-82.64,28.89,-82.76,29.0,-82.8,29.15,-82.99,29.18,-83.22,29.42,-83.4,29.52,-83.41,29.67,-83.54,29.72,-83.64,29.89,-84.02,30.1,-84.36,30.06,-84.34,29.9,-84.45,29.93,-84.87,29.74,-85.31,29.7,-85.3,29.81,-85.4,29.94,-85.92,30.24,-86.3,30.36,-86.63,30.4,-86.91,30.37,-87.52,30.28,-87.37,30.43,-87.45,30.51,-87.41,30.67,-87.63,30.87,-87.6,31.0,-85.5,31.0]],[[-83.11,35.0,-83.32,34.79,-83.34,34.68,-83.01,34.47,-82.9,34.49,-82.75,34.27,-82.71,34.15,-82.56,33.94,-82.33,33.82,-82.19,33.63,-81.93,33.46,-81.94,33.35,-81.76,33.16,-81.49,33.01,-81.43,32.84,-81.42,32.63,-81.28,32.56,-81.12,32.29,-81.12,32.12,-80.89,32.03,-81.13,31.69,-81.18,31.52,-81.28,31.36,-81.29,31.21,-81.4,31.13,-81.44,30.71,-81.72,30.75,-81.95,30.83,-82.04,30.75,-82.0,30.56,-82.05,30.36,-82.17,30.36,-82.22,30.57,-83.5,30.65,-84.87,30.71,-85.0,31.0,-85.11,31.28,-85.04,31.54,-85.14,31.84,-85.05,32.01,-85.06,32.14,-84.89,32.26,-85.0,32.32,-84.96,32.42,-85.07,32.58,-85.18,32.86,-85.43,34.12,-85.61,34.98,-84.32,34.99,-83.62,34.98,-83.11,35.0]],[[-116.05,49.0,-116.05,47.98,-115.72,47.7,-115.72,47.42,-115.53,47.3,-115.32,47.26,-115.3,47.19,-114.93,46.92,-114.89,46.81,-114.62,46.71,-114.61,46.64,-114.32,46.65,-114.46,46.27,-114.49,46.04,-114.39,45.88,-114.57,45.77,-114.5,45.67,-114.55,45.56,-114.33,45.46,-114.09,45.59,-113.99,45.7,-113.81,45.6,-113.83,45.52,-113.74,45.33,-113.57,45.13,-113.45,45.06,-113.46,44.87,-113.34,44.78,-113.13,44.77,-113.0,44.45,-112.89,44.39,-112.78,44.49,-112.47,44.48,-112.24,44.57,-112.1,44.52,-111.87,44.56,-111.82,44.51,-111.62,44.55,-111.39,44.76,-111.23,44.58,-111.05,44.48,-111.05,42.0,-112.16,42.0,-114.04,42.0,-117.03,42.0,-117.03,43.83,-116.9,44.16,-116.98,44.24,-117.17,44.26,-117.24,44.39,-117.04,44.75,-116.93,44.78,-116.83,44.93,-116.85,45.02,-116.73,45.14,-116.67,45.32,-116.46,45.62,-116.55,45.75,-116.78,45.82,-116.92,45.99,-116.92,46.17,-117.06,46.34,-117.04,46.43,-117.04,47.76,-117.03,49.0,-116.05,49.0]],[[-90.64,42.51,-88.79,42.49,-87.8,42.49,-87.84,42.3,-87.68,42.08,-87.52,41.71,-87.53,39.35,-87.64,39.17,-87.51,38.96,-87.5,38.78,-87.62,38.64,-87.66,38.51,-87.84,38.29,-87.95,38.28,-87.92,38.15,-88.0,38.1,-88.06,37.87,-88.03,37.8,-88.16,37.66,-88.07,37.48,-88.48,37.39,-88.51,37.29,-88.42,37.15,-88.55,37.07,-88.91,37.22,-89.03,37.21,-89.18,37.04,-89.13,36.98,-89.29,36.99,-89.52,37.28,-89.44,37.35,-89.52,37.54,-89.52,37.69,-89.84,37.9,-89.95,37.88,-90.06,38.01,-90.36,38.22,-90.35,38.37,-90.18,38.63,-90.21,38.73,-90.11,38.85,-90.25,38.92,-90.47,38.96,-90.59,38.87,-90.66,38.93,-90.73,39.26,-91.06,39.47,-91.37,39.73,-91.49,40.03,-91.51,40.24,-91.42,40.38,-91.4,40.56,-91.12,40.67,-91.09,40.82,-90.96,40.92,-90.95,41.1,-91.11,41.24,-91.05,41.41,-90.66,41.46,-90.34,41.59,-90.31,41.74,-90.18,41.81,-90.14,42.0,-90.17,42.13,-90.39,42.23,-90.42,42.33,-90.64,42.51]],[[-85.99,41.76,-84.81,41.76,-84.81,41.69,-84.8,40.5,-84.82,39.1,-84.89,39.06,-84.81,38.79,-84.99,38.78,-85.17,38.69,-85.43,38.73,-85.42,38.53,-85.59,38.45,-85.66,38.33,-85.83,38.28,-85.92,38.02,-86.04,37.96,-86.26,38.05,-86.3,38.17,-86.52,38.04,-86.5,37.93,-86.73,37.89,-86.8,37.99,-87.05,37.89,-87.13,37.79,-87.38,37.94,-87.51,37.9,-87.6,37.98,-87.68,37.9,-87.93,37.89,-88.03,37.8,-88.06,37.87,-88.0,38.1,-87.92,38.15,-87.95,38.28,-87.84,38.29,-87.66,38.51,-87.62,38.64,-87.5,38.78,-87.51,38.96,-87.64,39.17,-87.53,39.35,-87.52,41.71,-87.43,41.64,-87.12,41.64,-86.82,41.76,-85.99,41.76]],[[-91.37,43.5,-91.22,43.5,-91.2,43.35,-91.06,43.25,-91.18,43.13,-91.14,42.91,-91.07,42.75,-90.71,42.64,-90.64,42.51,-90.42,42.33,-90.39,42.23,-90.17,42.13,-90.14,42.0,-90.18,41.81,-90.31,41.74,-90.34,41.59,-90.66,41.46,-91.05,41.41,-91.11,41.24,-90.95,41.1,-90.96,40.92,-91.09,40.82,-91.12,40.67,-91.4,40.56,-91.42,40.38,-91.53,40.41,-91.73,40.62,-91.83,40.61,-93.26,40.58,-94.63,40.57,-95.77,40.59,-95.88,40.72,-95.83,40.98,-95.93,41.2,-95.92,41.45,-96.1,41.54,-96.12,41.68,-96.06,41.8,-96.13,41.97,-96.26,42.04,-96.45,42.49,-96.63,42.71,-96.54,42.86,-96.51,43.05,-96.43,43.12,-96.56,43.22,-96.53,43.4,-96.58,43.48,-96.45,43.5,-91.37,43.5]],[[-101.91,40.0,-95.31,40.0,-95.21,39.91,-94.88,39.83,-95.11,39.54,-94.98,39.44,-94.82,39.21,-94.61,39.16,-94.62,37.0,-100.09,37.0,-102.04,36.99,-102.05,40.0,-101.91,40.0]],[[-83.9,38.77,-83.68,38.63,-83.52,38.7,-83.14,38.63,-83.03,38.73,-82.89,38.76,-82.85,38.59,-82.73,38.56,-82.59,38.42,-82.62,38.12,-82.5,37.93,-82.34,37.78,-82.29,37.67,-82.1,37.55,-81.97,37.54,-82.35,37.27,-82.72,37.12,-82.72,37.04,-82.87,36.98,-82.88,36.89,-83.07,36.85,-83.14,36.74,-83.67,36.6,-83.69,36.58,-84.54,36.59,-85.29,36.63,-85.49,36.62,-86.59,36.66,-87.85,36.63,-88.07,36.68,-88.05,36.5,-89.3,36.51,-89.42,36.5,-89.36,36.62,-89.22,36.58,-89.13,36.98,-89.18,37.04,-89.03,37.21,-88.91,37.22,-88.55,37.07,-88.42,37.15,-88.51,37.29,-88.48,37.39,-88.07,37.48,-88.16,37.66,-88.03,37.8,-87.93,37.89,-87.68,37.9,-87.6,37.98,-87.51,37.9,-87.38,37.94,-87.13,37.79,-87.05,37.89,-86.8,37.99,-86.73,37.89,-86.5,37.93,-86.52,38.04,-86.3,38.17,-86.26,38.05,-86.04,37.96,-85.92,38.02,-85.83,38.28,-85.66,38.33,-85.59,38.45,-85.42,38.53,-85.43,38.73,-85.17,38.69,-84.99,38.78,-84.81,38.79,-84.89,39.06,-84.82,39.1,-84.43,39.1,-84.23,38.9,-84.22,38.81,-83.9,38.77]],[[-93.61,33.02,-91.17,33.0,-91.07,32.89,-91.14,32.84,-91.15,32.64,-91.01,32.51,-90.99,32.22,-91.11,31.99,-91.34,31.85,-91.4,31.62,-91.5,31.64,-91.52,31.28,-91.64,31.27,-91.57,31.07,-91.64,31.0,-89.75,31.0,-89.85,30.67,-89.68,30.45,-89.64,30.29,-89.52,30.18,-89.82,30.04,-89.84,29.95,-89.6,29.88,-89.5,30.04,-89.29,29.88,-89.3,29.75,-89.42,29.7,-89.65,29.75,-89.62,29.66,-89.7,29.51,-89.51,29.39,-89.2,29.35,-89.09,29.2,-89.0,29.18,-89.16,29.01,-89.34,29.04,-89.48,29.22,-89.85,29.31,-89.85,29.48,-90.03,29.43,-90.02,29.28,-90.1,29.15,-90.23,29.13,-90.33,29.28,-90.56,29.28,-90.65,29.13,-90.8,29.09,-90.96,29.18,-91.09,29.19,-91.22,29.44,-91.45,29.55,-91.53,29.53,-91.62,29.74,-91.88,29.71,-91.89,29.84,-92.15,29.72,-92.11,29.62,-92.31,29.54,-92.62,29.58,-92.97,29.72,-93.23,29.78,-93.77,29.73,-93.84,29.69,-93.93,29.79,-93.69,30.14,-93.77,30.33,-93.7,30.44,-93.73,30.58,-93.63,30.68,-93.53,30.94,-93.54,31.15,-93.82,31.56,-93.82,31.78,-94.04,31.99,-94.04,33.02,-93.61,33.02]],[[-70.7,43.06,-70.82,43.13,-70.81,43.23,-70.97,43.34,-71.03,44.66,-71.08,45.3,-70.65,45.44,-70.72,45.51,-70.56,45.66,-70.39,45.74,-70.42,45.8,-70.26,45.89,-70.31,46.06,-70.21,46.33,-70.06,46.42,-70.0,46.69,-69.23,47.46,-69.04,47.43,-69.03,47.24,-68.9,47.18,-68.58,47.29,-68.38,47.29,-68.23,47.36,-67.95,47.2,-67.79,47.07,-67.78,45.94,-67.8,45.68,-67.46,45.6,-67.51,45.49,-67.42,45.38,-67.49,45.28,-67.35,45.13,-67.16,45.16,-66.98,44.8,-67.19,44.65,-67.31,44.71,-67.41,44.6,-67.55,44.62,-67.57,44.53,-67.75,44.54,-68.05,44.33,-68.12,44.48,-68.22,44.49,-68.17,44.33,-68.4,44.25,-68.46,44.38,-68.57,44.31,-68.83,44.31,-68.83,44.46,-68.98,44.43,-68.96,44.32,-69.1,44.1,-69.07,44.04,-69.26,43.92,-69.44,43.97,-69.55,43.84,-69.71,43.82,-69.83,43.72,-69.99,43.74,-70.03,43.85,-70.25,43.68,-70.19,43.57,-70.36,43.53,-70.37,43.44,-70.56,43.32,-70.7,43.06]],[[-75.99,37.95,-76.02,37.95,-76.04,37.95,-75.99,37.95],[-79.48,39.72,-75.79,39.72,-75.69,38.46,-75.05,38.45,-75.24,38.03,-75.4,38.01,-75.67,37.95,-75.89,37.91,-75.88,38.07,-75.96,38.14,-75.85,38.21,-76.0,38.37,-76.05,38.3,-76.26,38.32,-76.33,38.5,-76.26,38.5,-76.26,38.74,-76.19,38.83,-76.28,39.15,-76.17,39.33,-76.0,39.37,-75.97,39.56,-76.1,39.54,-76.1,39.44,-76.37,39.31,-76.44,39.2,-76.46,38.91,-76.56,38.77,-76.51,38.54,-76.38,38.38,-76.4,38.26,-76.32,38.14,-76.36,38.06,-76.59,38.22,-76.92,38.29,-77.02,38.45,-77.21,38.36,-77.28,38.48,-77.13,38.63,-77.04,38.79,-76.91,38.9,-77.04,38.99,-77.12,38.93,-77.25,39.03,-77.46,39.08,-77.46,39.22,-77.57,39.31,-77.72,39.32,-77.83,39.6,-78.0,39.6,-78.17,39.69,-78.27,39.62,-78.43,39.62,-78.47,39.51,-78.77,39.59,-78.96,39.44,-79.09,39.47,-79.29,39.3,-79.49,39.21,-79.48,39.72]],[[-70.92,42.89,-70.82,42.87,-70.78,42.7,-70.82,42.55,-70.98,42.42,-70.99,42.27,-70.77,42.25,-70.64,42.09,-70.66,41.96,-70.55,41.93,-70.54,41.81,-70.26,41.72,-69.94,41.81,-70.01,41.67,-70.48,41.55,-70.66,41.55,-70.76,41.64,-70.93,41.61,-70.93,41.54,-71.12,41.5,-71.2,41.68,-71.22,41.71,-71.33,41.78,-71.38,42.02,-71.53,42.02,-71.8,42.01,-71.8,42.02,-73.05,42.04,-73.49,42.05,-73.51,42.09,-73.27,42.75,-72.46,42.73,-71.3,42.7,-71.19,42.79,-70.92,42.89]],[[-83.45,41.73,-84.81,41.69,-84.81,41.76,-85.99,41.76,-86.82,41.76,-86.62,41.89,-86.48,42.12,-86.36,42.25,-86.26,42.44,-86.21,42.72,-86.23,43.01,-86.53,43.59,-86.43,43.81,-86.5,44.08,-86.27,44.34,-86.22,44.57,-86.25,44.69,-86.09,44.74,-86.07,44.9,-85.81,44.95,-85.61,45.13,-85.63,44.77,-85.52,44.75,-85.39,44.93,-85.39,45.24,-85.31,45.31,-85.03,45.36,-85.12,45.58,-84.94,45.76,-84.71,45.77,-84.46,45.65,-84.22,45.64,-84.1,45.49,-83.91,45.48,-83.6,45.35,-83.49,45.36,-83.32,45.14,-83.45,45.03,-83.32,44.88,-83.27,44.71,-83.33,44.34,-83.54,44.25,-83.59,44.05,-83.83,43.99,-83.96,43.76,-83.91,43.67,-83.67,43.59,-83.48,43.71,-83.26,43.97,-82.92,44.07,-82.75,43.99,-82.64,43.85,-82.54,43.44,-82.52,43.23,-82.41,42.98,-82.52,42.61,-82.68,42.56,-82.69,42.69,-82.8,42.65,-82.92,42.35,-83.13,42.24,-83.19,42.01,-83.44,41.81,-83.45,41.73],[-85.51,45.73,-85.49,45.61,-85.62,45.59,-85.57,45.76,-85.51,45.73],[-87.59,45.1,-87.74,45.2,-87.65,45.34,-87.89,45.36,-87.79,45.5,-87.78,45.68,-87.99,45.8,-88.1,45.92,-88.53,46.02,-88.66,45.99,-89.09,46.14,-90.12,46.34,-90.23,46.51,-90.42,46.57,-90.03,46.67,-89.85,46.79,-89.41,46.84,-89.13,46.99,-89.0,47.0,-88.89,47.1,-88.58,47.25,-88.42,47.37,-88.18,47.46,-87.96,47.38,-88.35,47.08,-88.44,46.97,-88.44,46.79,-88.25,46.93,-87.9,46.91,-87.63,46.81,-87.39,46.54,-87.26,46.49,-87.01,46.53,-86.95,46.47,-86.7,46.44,-86.16,46.67,-85.88,46.69,-85.51,46.68,-85.26,46.75,-85.06,46.76,-85.03,46.48,-84.83,46.44,-84.63,46.49,-84.55,46.42,-84.42,46.5,-84.13,46.53,-84.12,46.18,-83.99,46.03,-83.79,45.99,-83.77,46.09,-83.58,46.09,-83.48,45.99,-83.56,45.91,-84.11,45.98,-84.37,45.93,-84.66,46.05,-84.74,45.94,-84.7,45.85,-84.83,45.87,-85.02,46.01,-85.34,46.09,-85.5,46.1,-85.66,45.97,-85.92,45.93,-86.21,45.96,-86.32,45.91,-86.35,45.8,-86.66,45.7,-86.65,45.83,-86.78,45.86,-86.84,45.73,-87.07,45.72,-87.17,45.66,-87.33,45.42,-87.61,45.12,-87.59,45.1],[-88.81,47.98,-89.06,47.85,-89.19,47.83,-89.18,47.94,-88.55,48.17,-88.67,48.01,-88.81,47.98]],[[-92.01,46.71,-92.09,46.75,-92.29,46.67,-92.29,46.08,-92.35,46.02,-92.64,45.93,-92.87,45.72,-92.89,45.58,-92.77,45.57,-92.64,45.44,-92.76,45.29,-92.74,45.12,-92.81,44.75,-92.55,44.57,-92.34,44.55,-92.23,44.44,-91.93,44.33,-91.88,44.2,-91.59,44.03,-91.43,43.99,-91.24,43.78,-91.27,43.62,-91.22,43.5,-91.37,43.5,-96.45,43.5,-96.45,45.3,-96.68,45.41,-96.86,45.6,-96.58,45.82,-96.56,45.93,-96.6,46.33,-96.72,46.44,-96.8,46.66,-96.79,46.92,-96.82,46.97,-96.86,47.61,-97.05,47.95,-97.13,48.14,-97.16,48.55,-97.1,48.68,-97.23,49.0,-95.15,49.0,-95.15,49.38,-94.96,49.37,-94.82,49.3,-94.69,48.78,-94.59,48.72,-94.26,48.7,-94.22,48.65,-93.84,48.63,-93.79,48.52,-93.47,48.55,-93.47,48.59,-93.21,48.64,-92.98,48.62,-92.73,48.54,-92.66,48.44,-92.51,48.45,-92.37,48.22,-92.3,48.32,-92.05,48.36,-92.01,48.27,-91.71,48.2,-91.71,48.11,-91.57,48.04,-91.26,48.08,-91.08,48.18,-90.84,48.24,-90.75,48.09,-90.58,48.12,-90.38,48.09,-90.14,48.11,-89.87,47.99,-89.62,48.01,-89.64,47.95,-89.97,47.83,-90.44,47.73,-90.74,47.63,-91.17,47.37,-91.36,47.21,-91.64,47.03,-92.09,46.79,-92.01,46.71]],[[-88.47,35.0,-88.2,35.0,-88.1,34.89,-88.24,33.8,-88.47,31.9,-88.39,30.37,-88.5,30.32,-88.74,30.35,-88.84,30.41,-89.08,30.37,-89.42,30.25,-89.52,30.18,-89.64,30.29,-89.68,30.45,-89.85,30.67,-89.75,31.0,-91.64,31.0,-91.57,31.07,-91.64,31.27,-91.52,31.28,-91.5,31.64,-91.4,31.62,-91.34,31.85,-91.11,31.99,-90.99,32.22,-91.01,32.51,-91.15,32.64,-91.14,32.84,-91.07,32.89,-91.17,33.0,-91.09,33.14,-91.14,33.35,-91.06,33.43,-91.23,33.56,-91.07,33.87,-90.89,34.03,-90.95,34.14,-90.74,34.3,-90.75,34.37,-90.57,34.42,-90.59,34.62,-90.48,34.66,-90.41,34.83,-90.25,34.91,-90.31,35.0,-88.47,35.0]],[[-91.83,40.61,-91.73,40.62,-91.53,40.41,-91.42,40.38,-91.51,40.24,-91.49,40.03,-91.37,39.73,-91.06,39.47,-90.73,39.26,-90.66,38.93,-90.59,38.87,-90.47,38.96,-90.25,38.92,-90.11,38.85,-90.21,38.73,-90.18,38.63,-90.35,38.37,-90.36,38.22,-90.06,38.01,-89.95,37.88,-89.84,37.9,-89.52,37.69,-89.52,37.54,-89.44,37.35,-89.52,37.28,-89.29,36.99,-89.13,36.98,-89.22,36.58,-89.36,36.62,-89.42,36.5,-89.48,36.5,-89.54,36.5,-89.53,36.25,-89.73,36.0,-90.38,36.0,-90.22,36.18,-90.06,36.3,-90.15,36.5,-94.47,36.5,-94.62,36.5,-94.62,37.0,-94.61,39.16,-94.82,39.21,-94.98,39.44,-95.11,39.54,-94.88,39.83,-95.21,39.91,-95.31,40.0,-95.55,40.26,-95.77,40.59,-94.63,40.57,-93.26,40.58,-91.83,40.61]],[[-104.05,49.0,-104.04,47.86,-104.05,45.94,-104.04,45.0,-104.06,45.0,-105.92,45.0,-109.08,45.0,-111.05,45.0,-111.05,44.48,-111.23,44.58,-111.39,44.76,-111.62,44.55,-111.82,44.51,-111.87,44.56,-112.1,44.52,-112.24,44.57,-112.47,44.48,-112.78,44.49,-112.89,44.39,-113.0,44.45,-113.13,44.77,-113.34,44.78,-113.46,44.87,-113.45,45.06,-113.57,45.13,-113.74,45.33,-113.83,45.52,-113.81,45.6,-113.99,45.7,-114.09,45.59,-114.33,45.46,-114.55,45.56,-114.5,45.67,-114.57,45.77,-114.39,45.88,-114.49,46.04,-114.46,46.27,-114.32,46.65,-114.61,46.64,-114.62,46.71,-114.89,46.81,-114.93,46.92,-115.3,47.19,-115.32,47.26,-115.53,47.3,-115.72,47.42,-115.72,47.7,-116.05,47.98,-116.05,49.0,-111.5,48.99,-109.45,49.0,-104.05,49.0]],[[-103.32,43.0,-101.63,43.0,-98.5,43.0,-98.47,42.95,-97.95,42.77,-97.83,42.87,-97.69,42.84,-97.22,42.84,-96.69,42.66,-96.63,42.52,-96.45,42.49,-96.26,42.04,-96.13,41.97,-96.06,41.8,-96.12,41.68,-96.1,41.54,-95.92,41.45,-95.93,41.2,-95.83,40.98,-95.88,40.72,-95.77,40.59,-95.55,40.26,-95.31,40.0,-101.91,40.0,-102.05,40.0,-102.05,41.0,-104.05,41.0,-104.05,43.0,-103.32,43.0]],[[-117.03,42.0,-114.04,42.0,-114.05,37.0,-114.05,36.2,-114.15,36.03,-114.25,36.02,-114.37,36.14,-114.74,36.1,-114.68,35.52,-114.6,35.32,-114.57,35.14,-114.63,35.0,-115.85,35.97,-116.54,36.5,-117.5,37.22,-118.71,38.1,-120.0,39.0,-120.0,40.26,-120.0,42.0,-118.7,41.99,-117.03,42.0]],[[-71.08,45.3,-71.03,44.66,-70.97,43.34,-70.81,43.23,-70.82,43.13,-70.7,43.06,-70.82,42.87,-70.92,42.89,-71.19,42.79,-71.3,42.7,-72.46,42.73,-72.54,42.81,-72.53,42.95,-72.45,43.01,-72.46,43.15,-72.38,43.57,-72.2,43.77,-72.12,43.99,-72.03,44.08,-72.03,44.32,-71.7,44.42,-71.54,44.59,-71.63,44.75,-71.49,44.91,-71.5,45.01,-71.36,45.27,-71.13,45.24,-71.08,45.3]],[[-74.24,41.14,-73.9,41.0,-74.02,40.71,-74.19,40.64,-74.27,40.49,-74.0,40.41,-73.98,40.3,-74.1,39.76,-74.41,39.36,-74.61,39.25,-74.8,38.99,-74.89,39.16,-75.18,39.24,-75.53,39.46,-75.56,39.61,-75.56,39.63,-75.51,39.68,-75.41,39.8,-75.15,39.89,-75.13,39.96,-74.82,40.13,-74.77,40.22,-75.06,40.42,-75.07,40.54,-75.2,40.58,-75.21,40.69,-75.05,40.87,-75.13,40.97,-74.88,41.18,-74.83,41.29,-74.7,41.36,-74.24,41.14]],[[-107.42,37.0,-106.87,36.99,-104.34,36.99,-103.0,37.0,-103.0,36.5,-103.04,36.5,-103.05,34.02,-103.07,33.0,-103.07,32.0,-106.62,32.0,-106.64,31.9,-106.53,31.79,-108.21,31.79,-108.21,31.33,-109.05,31.33,-109.04,37.0,-107.42,37.0]],[[-73.34,45.01,-73.33,44.8,-73.39,44.62,-73.29,44.44,-73.32,44.25,-73.44,44.04,-73.35,43.77,-73.4,43.69,-73.25,43.52,-73.28,42.83,-73.27,42.75,-73.51,42.09,-73.49,42.05,-73.55,41.29,-73.48,41.21,-73.73,41.1,-73.66,40.99,-73.23,40.91,-73.14,40.97,-72.77,40.97,-72.59,41.0,-72.28,41.16,-72.26,41.04,-72.1,40.99,-72.47,40.85,-73.24,40.63,-73.56,40.58,-73.78,40.59,-73.94,40.54,-74.02,40.71,-73.9,41.0,-74.24,41.14,-74.7,41.36,-74.74,41.43,-74.89,41.44,-75.07,41.61,-75.05,41.75,-75.17,41.87,-75.25,41.86,-75.36,42.0,-79.76,42.0,-79.76,42.25,-79.76,42.27,-79.15,42.55,-79.05,42.69,-78.85,42.78,-78.93,42.95,-79.01,42.99,-79.07,43.26,-78.49,43.38,-77.97,43.37,-77.76,43.34,-77.53,43.23,-77.39,43.28,-76.96,43.27,-76.7,43.34,-76.42,43.52,-76.24,43.53,-76.23,43.8,-76.14,43.96,-76.36,44.07,-76.31,44.2,-75.91,44.37,-75.76,44.51,-75.28,44.85,-74.83,45.02,-74.15,44.99,-73.34,45.01]],[[-80.98,36.56,-80.29,36.55,-79.51,36.54,-75.87,36.55,-75.75,36.15,-76.03,36.19,-76.07,36.14,-76.41,36.08,-76.46,36.03,-76.68,36.01,-76.67,35.94,-76.4,35.99,-76.36,35.94,-76.06,35.99,-75.96,35.9,-75.78,35.94,-75.72,35.7,-75.78,35.58,-75.9,35.57,-76.15,35.32,-76.48,35.31,-76.54,35.14,-76.39,34.97,-76.28,34.94,-76.49,34.66,-76.67,34.69,-76.99,34.67,-77.21,34.61,-77.56,34.42,-77.83,34.16,-77.97,33.85,-78.18,33.92,-78.54,33.85,-79.68,34.8,-80.8,34.82,-80.78,34.94,-80.93,35.11,-81.04,35.04,-81.04,35.15,-82.28,35.2,-82.55,35.16,-82.76,35.07,-83.11,35.0,-83.62,34.98,-84.32,34.99,-84.29,35.23,-84.1,35.25,-84.02,35.41,-83.77,35.56,-83.5,35.57,-83.25,35.72,-82.99,35.77,-82.78,36.0,-82.64,36.06,-82.61,35.97,-82.22,36.16,-82.04,36.12,-81.91,36.3,-81.72,36.35,-81.68,36.59,-80.98,36.56]],[[-97.23,49.0,-97.1,48.68,-97.16,48.55,-97.13,48.14,-97.05,47.95,-96.86,47.61,-96.82,46.97,-96.79,46.92,-96.8,46.66,-96.72,46.44,-96.6,46.33,-96.56,45.93,-104.05,45.94,-104.04,47.86,-104.05,49.0,-97.23,49.0]],[[-80.52,41.98,-80.52,40.64,-80.67,40.58,-80.6,40.47,-80.6,40.32,-80.74,40.08,-80.83,39.71,-81.22,39.39,-81.35,39.34,-81.46,39.41,-81.57,39.27,-81.69,39.27,-81.81,39.08,-81.78,38.97,-81.89,38.87,-82.04,39.03,-82.22,38.79,-82.17,38.63,-82.29,38.58,-82.33,38.45,-82.59,38.42,-82.73,38.56,-82.85,38.59,-82.89,38.76,-83.03,38.73,-83.14,38.63,-83.52,38.7,-83.68,38.63,-83.9,38.77,-84.22,38.81,-84.23,38.9,-84.43,39.1,-84.82,39.1,-84.8,40.5,-84.81,41.69,-83.45,41.73,-83.07,41.6,-82.93,41.51,-82.84,41.59,-82.62,41.43,-82.48,41.38,-82.01,41.51,-81.74,41.49,-81.44,41.67,-81.01,41.85,-80.52,41.98]],[[-100.09,37.0,-94.62,37.0,-94.62,36.5,-94.43,35.4,-94.48,33.64,-94.87,33.75,-94.97,33.86,-95.22,33.96,-95.29,33.87,-95.55,33.88,-95.6,33.93,-95.84,33.83,-95.94,33.89,-96.15,33.84,-96.35,33.69,-96.42,33.77,-96.63,33.85,-96.85,33.85,-96.92,33.96,-97.17,33.74,-97.26,33.86,-97.37,33.82,-97.46,33.91,-97.69,33.98,-97.87,33.85,-97.95,33.99,-98.09,34.0,-98.17,34.11,-98.36,34.16,-98.49,34.06,-98.57,34.15,-98.77,34.14,-98.99,34.22,-99.19,34.21,-99.26,34.4,-99.58,34.42,-99.7,34.38,-99.92,34.57,-100.0,34.56,-100.0,36.5,-101.81,36.5,-103.0,36.5,-103.0,37.0,-102.04,36.99,-100.09,37.0]],[[-123.21,46.17,-123.12,46.19,-122.9,46.08,-122.81,45.96,-122.76,45.66,-122.25,45.55,-121.81,45.71,-121.54,45.73,-121.22,45.67,-121.18,45.6,-120.64,45.75,-120.51,45.7,-120.21,45.73,-119.96,45.82,-119.53,45.91,-119.13,45.93,-118.99,46.0,-116.92,45.99,-116.78,45.82,-116.55,45.75,-116.46,45.62,-116.67,45.32,-116.73,45.14,-116.85,45.02,-116.83,44.93,-116.93,44.78,-117.04,44.75,-117.24,44.39,-117.17,44.26,-116.98,44.24,-116.9,44.16,-117.03,43.83,-117.03,42.0,-118.7,41.99,-120.0,42.0,-121.04,42.0,-122.38,42.01,-123.23,42.01,-124.21,42.0,-124.36,42.12,-124.43,42.44,-124.42,42.66,-124.55,42.84,-124.45,43.0,-124.38,43.27,-124.24,43.56,-124.17,43.81,-124.06,44.66,-124.08,44.77,-123.98,45.14,-123.94,45.66,-123.99,45.94,-123.95,46.11,-123.55,46.26,-123.37,46.15,-123.21,46.17]],[[-79.76,42.25,-79.76,42.0,-75.36,42.0,-75.25,41.86,-75.17,41.87,-75.05,41.75,-75.07,41.61,-74.89,41.44,-74.74,41.43,-74.7,41.36,-74.83,41.29,-74.88,41.18,-75.13,40.97,-75.05,40.87,-75.21,40.69,-75.2,40.58,-75.07,40.54,-75.06,40.42,-74.77,40.22,-74.82,40.13,-75.13,39.96,-75.15,39.89,-75.41,39.8,-75.62,39.83,-75.79,39.72,-79.48,39.72,-80.52,39.72,-80.52,40.64,-80.52,41.98,-80.33,42.03,-79.76,42.27,-79.76,42.25]],[[-71.2,41.68,-71.12,41.5,-71.32,41.47,-71.2,41.68],[-71.53,42.02,-71.38,42.02,-71.33,41.78,-71.22,41.71,-71.34,41.73,-71.45,41.58,-71.48,41.37,-71.86,41.32,-71.8,41.41,-71.8,42.01,-71.53,42.02]],[[-82.76,35.07,-82.55,35.16,-82.28,35.2,-81.04,35.15,-81.04,35.04,-80.93,35.11,-80.78,34.94,-80.8,34.82,-79.68,34.8,-78.54,33.85,-78.72,33.8,-78.94,33.64,-79.15,33.38,-79.19,33.17,-79.36,33.01,-79.58,33.01,-79.63,32.89,-79.87,32.76,-80.0,32.61,-80.21,32.55,-80.43,32.4,-80.45,32.33,-80.66,32.25,-80.89,32.03,-81.12,32.12,-81.12,32.29,-81.28,32.56,-81.42,32.63,-81.43,32.84,-81.49,33.01,-81.76,33.16,-81.94,33.35,-81.93,33.46,-82.19,33.63,-82.33,33.82,-82.56,33.94,-82.71,34.15,-82.75,34.27,-82.9,34.49,-83.01,34.47,-83.34,34.68,-83.32,34.79,-83.11,35.0,-82.76,35.07]],[[-104.05,45.94,-96.56,45.93,-96.58,45.82,-96.86,45.6,-96.68,45.41,-96.45,45.3,-96.45,43.5,-96.58,43.48,-96.53,43.4,-96.56,43.22,-96.43,43.12,-96.51,43.05,-96.54,42.86,-96.63,42.71,-96.45,42.49,-96.63,42.52,-96.69,42.66,-97.22,42.84,-97.69,42.84,-97.83,42.87,-97.95,42.77,-98.47,42.95,-98.5,43.0,-101.63,43.0,-103.32,43.0,-104.05,43.0,-104.06,45.0,-104.04,45.0,-104.05,45.94]],[[-88.05,36.5,-88.07,36.68,-87.85,36.63,-86.59,36.66,-85.49,36.62,-85.29,36.63,-84.54,36.59,-83.69,36.58,-83.67,36.6,-81.68,36.59,-81.72,36.35,-81.91,36.3,-82.04,36.12,-82.22,36.16,-82.61,35.97,-82.64,36.06,-82.78,36.0,-82.99,35.77,-83.25,35.72,-83.5,35.57,-83.77,35.56,-84.02,35.41,-84.1,35.25,-84.29,35.23,-84.32,34.99,-85.61,34.98,-87.36,35.0,-88.2,35.0,-88.47,35.0,-90.31,35.0,-90.21,35.02,-90.11,35.2,-90.13,35.44,-89.94,35.6,-89.91,35.76,-89.76,35.81,-89.73,36.0,-89.53,36.25,-89.54,36.5,-89.48,36.5,-89.42,36.5,-89.3,36.51,-88.05,36.5]],[[-101.81,36.5,-100.0,36.5,-100.0,34.56,-99.92,34.57,-99.7,34.38,-99.58,34.42,-99.26,34.4,-99.19,34.21,-98.99,34.22,-98.77,34.14,-98.57,34.15,-98.49,34.06,-98.36,34.16,-98.17,34.11,-98.09,34.0,-97.95,33.99,-97.87,33.85,-97.69,33.98,-97.46,33.91,-97.37,33.82,-97.26,33.86,-97.17,33.74,-96.92,33.96,-96.85,33.85,-96.63,33.85,-96.42,33.77,-96.35,33.69,-96.15,33.84,-95.94,33.89,-95.84,33.83,-95.6,33.93,-95.55,33.88,-95.29,33.87,-95.22,33.96,-94.97,33.86,-94.87,33.75,-94.48,33.64,-94.38,33.54,-94.18,33.59,-94.04,33.55,-94.04,33.02,-94.04,31.99,-93.82,31.78,-93.82,31.56,-93.54,31.15,-93.53,30.94,-93.63,30.68,-93.73,30.58,-93.7,30.44,-93.77,30.33,-93.69,30.14,-93.93,29.79,-93.84,29.69,-94.0,29.68,-94.52,29.55,-94.71,29.62,-94.74,29.79,-94.87,29.67,-94.97,29.7,-95.02,29.56,-94.91,29.5,-94.9,29.31,-95.08,29.11,-95.38,28.87,-95.99,28.6,-96.05,28.65,-96.23,28.58,-96.23,28.64,-96.48,28.6,-96.59,28.72,-96.66,28.7,-96.4,28.44,-96.59,28.36,-96.77,28.41,-96.8,28.23,-97.03,28.04,-97.26,27.69,-97.4,27.33,-97.51,27.36,-97.54,27.23,-97.43,27.26,-97.48,27.0,-97.56,26.99,-97.56,26.84,-97.47,26.76,-97.44,26.46,-97.33,26.35,-97.31,26.16,-97.22,25.99,-97.52,25.89,-97.65,26.02,-97.89,26.07,-98.2,26.06,-98.47,26.22,-98.67,26.24,-98.82,26.37,-99.03,26.41,-99.17,26.54,-99.27,26.84,-99.45,27.02,-99.42,27.17,-99.51,27.34,-99.48,27.48,-99.61,27.64,-99.71,27.66,-99.88,27.8,-99.93,27.98,-100.08,28.14,-100.3,28.28,-100.4,28.58,-100.5,28.66,-100.63,28.91,-100.67,29.1,-100.8,29.24,-101.01,29.37,-101.06,29.46,-101.26,29.54,-101.41,29.75,-101.85,29.8,-102.11,29.79,-102.34,29.87,-102.39,29.77,-102.63,29.73,-102.81,29.52,-102.92,29.19,-102.98,29.18,-103.12,28.99,-103.28,28.98,-103.53,29.14,-104.15,29.38,-104.27,29.51,-104.51,29.64,-104.68,29.92,-104.69,30.18,-104.86,30.39,-104.9,30.57,-105.01,30.69,-105.39,30.86,-105.6,31.09,-105.77,31.17,-105.95,31.36,-106.21,31.47,-106.38,31.73,-106.53,31.79,-106.64,31.9,-106.62,32.0,-103.07,32.0,-103.07,33.0,-103.05,34.02,-103.04,36.5,-103.0,36.5,-101.81,36.5]],[[-112.16,42.0,-111.05,42.0,-111.05,41.0,-109.05,41.0,-109.05,39.13,-109.06,38.28,-109.04,38.17,-109.04,37.0,-110.5,37.01,-114.05,37.0,-114.04,42.0,-112.16,42.0]],[[-71.5,45.01,-71.49,44.91,-71.63,44.75,-71.54,44.59,-71.7,44.42,-72.03,44.32,-72.03,44.08,-72.12,43.99,-72.2,43.77,-72.38,43.57,-72.46,43.15,-72.45,43.01,-72.53,42.95,-72.54,42.81,-72.46,42.73,-73.27,42.75,-73.28,42.83,-73.25,43.52,-73.4,43.69,-73.35,43.77,-73.44,44.04,-73.32,44.25,-73.29,44.44,-73.39,44.62,-73.33,44.8,-73.34,45.01,-72.31,45.0,-71.5,45.01]],[[-75.4,38.01,-75.24,38.03,-75.38,37.86,-75.51,37.8,-75.59,37.57,-75.8,37.2,-75.97,37.12,-76.03,37.26,-75.94,37.56,-75.67,37.95,-75.4,38.01],[-76.02,37.95,-75.99,37.95,-76.04,37.95,-76.02,37.95],[-78.35,39.46,-77.83,39.13,-77.72,39.32,-77.57,39.31,-77.46,39.22,-77.46,39.08,-77.25,39.03,-77.12,38.93,-77.04,38.79,-77.13,38.63,-77.25,38.59,-77.33,38.45,-77.28,38.34,-77.01,38.37,-76.96,38.22,-76.61,38.15,-76.51,38.02,-76.24,37.89,-76.36,37.61,-76.25,37.39,-76.38,37.29,-76.4,37.16,-76.27,37.08,-76.41,36.96,-76.62,37.12,-76.67,37.07,-76.49,36.95,-75.99,36.92,-75.87,36.55,-79.51,36.54,-80.29,36.55,-80.98,36.56,-81.68,36.59,-83.67,36.6,-83.14,36.74,-83.07,36.85,-82.88,36.89,-82.87,36.98,-82.72,37.04,-82.72,37.12,-82.35,37.27,-81.97,37.54,-81.99,37.45,-81.85,37.29,-81.68,37.2,-81.55,37.21,-81.36,37.34,-81.23,37.24,-80.97,37.29,-80.51,37.48,-80.47,37.42,-80.3,37.51,-80.29,37.69,-80.18,37.85,-80.0,38.0,-79.92,38.18,-79.72,38.36,-79.65,38.59,-79.48,38.46,-79.31,38.41,-79.21,38.5,-79.0,38.85,-78.87,38.76,-78.4,39.17,-78.35,39.46]],[[-117.03,49.0,-117.04,47.76,-117.04,46.43,-117.06,46.34,-116.92,46.17,-116.92,45.99,-118.99,46.0,-119.13,45.93,-119.53,45.91,-119.96,45.82,-120.21,45.73,-120.51,45.7,-120.64,45.75,-121.18,45.6,-121.22,45.67,-121.54,45.73,-121.81,45.71,-122.25,45.55,-122.76,45.66,-122.81,45.96,-122.9,46.08,-123.12,46.19,-123.21,46.17,-123.37,46.15,-123.55,46.26,-123.73,46.3,-123.87,46.24,-124.07,46.33,-124.03,46.46,-123.9,46.54,-124.1,46.74,-124.24,47.29,-124.32,47.36,-124.43,47.74,-124.62,47.89,-124.71,48.18,-124.6,48.38,-124.39,48.29,-123.98,48.16,-123.7,48.17,-123.42,48.12,-123.16,48.17,-123.04,48.08,-122.8,48.09,-122.64,47.87,-122.52,47.88,-122.49,47.59,-122.42,47.32,-122.32,47.35,-122.42,47.58,-122.4,47.8,-122.23,48.03,-122.36,48.12,-122.37,48.29,-122.47,48.47,-122.42,48.6,-122.49,48.75,-122.65,48.78,-122.8,48.89,-122.76,49.0,-117.03,49.0],[-122.72,48.31,-122.59,48.35,-122.61,48.15,-122.77,48.23,-122.72,48.31],[-123.03,48.58,-122.92,48.72,-122.77,48.56,-122.81,48.42,-123.04,48.46,-123.03,48.58]],[[-80.52,40.64,-80.52,39.72,-79.48,39.72,-79.49,39.21,-79.29,39.3,-79.09,39.47,-78.96,39.44,-78.77,39.59,-78.47,39.51,-78.43,39.62,-78.27,39.62,-78.17,39.69,-78.0,39.6,-77.83,39.6,-77.72,39.32,-77.83,39.13,-78.35,39.46,-78.4,39.17,-78.87,38.76,-79.0,38.85,-79.21,38.5,-79.31,38.41,-79.48,38.46,-79.65,38.59,-79.72,38.36,-79.92,38.18,-80.0,38.0,-80.18,37.85,-80.29,37.69,-80.3,37.51,-80.47,37.42,-80.51,37.48,-80.97,37.29,-81.23,37.24,-81.36,37.34,-81.55,37.21,-81.68,37.2,-81.85,37.29,-81.99,37.45,-81.97,37.54,-82.1,37.55,-82.29,37.67,-82.34,37.78,-82.5,37.93,-82.62,38.12,-82.59,38.42,-82.33,38.45,-82.29,38.58,-82.17,38.63,-82.22,38.79,-82.04,39.03,-81.89,38.87,-81.78,38.97,-81.81,39.08,-81.69,39.27,-81.57,39.27,-81.46,39.41,-81.35,39.34,-81.22,39.39,-80.83,39.71,-80.74,40.08,-80.6,40.32,-80.6,40.47,-80.67,40.58,-80.52,40.64]],[[-90.42,46.57,-90.23,46.51,-90.12,46.34,-89.09,46.14,-88.66,45.99,-88.53,46.02,-88.1,45.92,-87.99,45.8,-87.78,45.68,-87.79,45.5,-87.89,45.36,-87.65,45.34,-87.74,45.2,-87.59,45.1,-87.63,44.97,-87.82,44.95,-87.98,44.72,-88.04,44.56,-87.93,44.54,-87.78,44.64,-87.61,44.84,-87.4,44.91,-87.24,45.17,-87.03,45.22,-87.05,45.09,-87.19,44.97,-87.47,44.55,-87.55,44.32,-87.54,44.16,-87.64,44.1,-87.74,43.88,-87.7,43.69,-87.79,43.56,-87.91,43.25,-87.89,43.0,-87.76,42.78,-87.8,42.49,-88.79,42.49,-90.64,42.51,-90.71,42.64,-91.07,42.75,-91.14,42.91,-91.18,43.13,-91.06,43.25,-91.2,43.35,-91.22,43.5,-91.27,43.62,-91.24,43.78,-91.43,43.99,-91.59,44.03,-91.88,44.2,-91.93,44.33,-92.23,44.44,-92.34,44.55,-92.55,44.57,-92.81,44.75,-92.74,45.12,-92.76,45.29,-92.64,45.44,-92.77,45.57,-92.89,45.58,-92.87,45.72,-92.64,45.93,-92.35,46.02,-92.29,46.08,-92.29,46.67,-92.09,46.75,-92.01,46.71,-91.79,46.69,-91.09,46.86,-90.84,46.96,-90.75,46.89,-90.89,46.75,-90.56,46.58,-90.42,46.57]],[[-109.08,45.0,-105.92,45.0,-104.06,45.0,-104.05,43.0,-104.05,41.0,-105.73,41.0,-107.92,41.0,-109.05,41.0,-111.05,41.0,-111.05,42.0,-111.05,44.48,-111.05,45.0,-109.08,45.0]]]"""


@app.get("/map")
def crawl_map():
    return _render(MAP_PAGE, outline=US_OUTLINE)


@app.get("/map/data")
def crawl_map_data():
    """Every city JARVIS has placed on the map, and what he found there.

    Only cities he has actually looked up appear — the coordinates are
    Google's, not guesses, so the map fills in as he works rather than
    pretending to know where everywhere is up front.
    """
    db = STATE.db
    try:
        cities = json.loads(db.get_kv("crawl_cities") or "[]")
        at = int(db.get_kv("crawl_city") or 0)
    except (TypeError, ValueError):
        cities, at = [], 0

    points, total = [], 0
    for i, city in enumerate(cities):
        placed = db.get_kv("geo:" + city.lower().strip())
        if not placed:
            continue                      # not reached yet: nothing to draw
        try:
            lat, lng = (float(x) for x in placed.split(","))
        except ValueError:
            continue
        try:
            found = int(db.get_kv("found:" + city.lower().strip()) or 0)
        except ValueError:
            found = 0
        total += found
        points.append({"city": city, "lat": lat, "lng": lng, "found": found,
                       "state": city.rsplit(", ", 1)[-1],
                       "now": i == at, "done": i < at})
    here = cities[at] if at < len(cities) else ""
    return {"points": points, "at": at + 1 if cities else 0,
            "of": len(cities), "here": here, "found": total,
            "bounds": {"w": MAP_W, "e": MAP_E, "n": MAP_N, "s": MAP_S,
                       "aspect": MAP_ASPECT},
            "working": bool(STATE.config.get("auto_search_enabled")
                            and STATE.config.get("autopilot_enabled"))}


@app.get("/jarvis")
def jarvis():
    return JARVIS.replace("PWAMETA_PLACEHOLDER", PWA_META)


@app.get("/jarvis/data")
def jarvis_data():
    db = STATE.db
    cfg = STATE.config
    leads = db.all_leads()
    stages = {}
    for lead in leads:
        stages[lead["stage"]] = stages.get(lead["stage"], 0) + 1

    def st(*names):
        return sum(stages.get(s, 0) for s in names)

    active = st(core.STAGE_CONTACTED, core.STAGE_BUILDING_PREVIEW,
                core.STAGE_PREVIEW_SENT, core.STAGE_SENDING_PAYMENT_LINK,
                core.STAGE_PAYMENT_LINK_SENT, core.STAGE_PAID,
                core.STAGE_DEPLOYING_FINAL)
    # "interested" = made it past the cold email, into preview or beyond
    interested = st(core.STAGE_BUILDING_PREVIEW, core.STAGE_PREVIEW_SENT,
                    core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT,
                    core.STAGE_PAID, core.STAGE_DEPLOYING_FINAL,
                    core.STAGE_DELIVERED)
    paid_count = sum(1 for lead in leads if lead["paid_at"])
    delivered = stages.get(core.STAGE_DELIVERED, 0)

    ev = db.event_counts()
    outreach = ev.get("outreach_sent", 0)
    replies = ev.get("reply_received", 0)
    replied_leads = db.distinct_replied_leads()
    previews = ev.get("preview_emailed", 0)
    links = ev.get("payment_link_emailed", 0)
    emails_total = (outreach + previews + links + ev.get("delivered", 0)
                    + ev.get("reply_after_link", 0))
    response_rate = min(100, round(100 * replied_leads / outreach)) if outreach else 0
    conversion = min(100, round(100 * paid_count / outreach)) if outreach else 0

    revenue = db.revenue_cents() // 100
    pending = db.pending_cents() // 100
    price = float(cfg.get("site_price_usd", 500) or 0)
    pipeline_value = int(active * price)
    avg_deal = revenue // paid_count if paid_count else 0

    def usd(n):
        return "$" + f"{n:,}"

    money = [
        {"l": "Revenue collected", "v": usd(revenue),
         "s": f"{paid_count} paid project" + ("" if paid_count == 1 else "s")},
        {"l": "Awaiting payment", "v": usd(pending),
         "s": f"{stages.get(core.STAGE_PAYMENT_LINK_SENT, 0)} payment links out"},
        {"l": "Pipeline value", "v": usd(pipeline_value),
         "s": f"{active} active deals × ${core.fmt_price(price)}"},
        {"l": "Avg project", "v": usd(avg_deal), "s": "per paid deal"},
    ]
    kpis = [
        {"l": "Leads", "v": len(leads), "hot": True},
        {"l": "Cold emails", "v": outreach},
        {"l": "Emails total", "v": emails_total},
        {"l": "Replies", "v": replies, "hot": True},
        {"l": "Response rate", "v": f"{response_rate}%"},
        {"l": "Interested", "v": interested, "hot": True},
        {"l": "Previews sent", "v": previews},
        {"l": "Pay links sent", "v": links},
        {"l": "Deals won", "v": paid_count, "hot": True},
        {"l": "Conversion", "v": f"{conversion}%"},
        {"l": "Sites live", "v": delivered},
        {"l": "Passed", "v": stages.get(core.STAGE_NOT_INTERESTED, 0)},
        {"l": "Searches run", "v": ev.get("find_leads", 0)},
        {"l": "Awaiting approval", "v": len(db.leads_awaiting_approval()),
         "hot": True},
        {"l": "Need email", "v": len(db.leads_needing_email())},
    ]

    events = []
    for e in db.recent_events(12):
        events.append({
            "time": (e["created_at"] or "")[11:19] or "--:--:--",
            "kind": e["kind"],
            "detail": (e["detail"] or "")[:160],
        })
    return {
        "owner": (cfg.get("your_name") or "").split(" ")[0] or None,
        "autopilot": bool(cfg.get("autopilot_enabled")),
        "attention": len(db.attention_events()),
        "findings": current_findings(),
        "money": money,
        "kpis": kpis,
        "stages": {s: stages[s] for s in core.ALL_STAGES if s in stages},
        "events": events,
    }


@app.get("/")
def dashboard():
    db = STATE.db
    leads = db.all_leads()
    counts = {}
    for l in leads:
        counts[l["stage"]] = counts.get(l["stage"], 0) + 1
    order = [s for s in core.ALL_STAGES if s in counts]
    cfg = STATE.config
    configured = bool(cfg.get("inkbox_api_key") and cfg.get("anthropic_api_key"))
    return _render(DASHBOARD, leads=leads, today_line=core.line_for_today(),
                   stage_counts=[(s, counts[s]) for s in order],
                   attention=db.attention_events(), configured=configured,
                   findings=current_findings())



APPROVE_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Approve outreach</h1>
<p class="muted" style="margin-top:-6px">
Nothing is emailed until you approve it here.
Sent today: <b>{{ sent_today }}</b>{% if cap %} of {{ cap }}{% endif %}.
{% if cap and sent_today >= cap %}
<span style="color:var(--bad)">Daily limit reached — the rest wait for tomorrow.</span>
{% endif %}
</p>
<div class="filters">
  <span class="muted">Show:</span>
  {% for key, label in [('', 'Everyone'), ('none', 'Strict'),
                        ('broken', 'Normal'), ('weak', 'Wide')] %}
    <a class="chip {% if only == key %}on{% endif %}"
       href="{{ url_for('approve_queue') }}{% if key %}?only={{ key }}{% endif %}"
       >{{ label }}</a>
  {% endfor %}
  <span class="muted" style="margin-left:auto">
    {% for key, n in counts.items() %}{{ n }} {{ reasons.get(key, key) }}{% if not loop.last %} · {% endif %}{% endfor %}
  </span>
</div>
{% if queue %}
<form method="post" action="{{ url_for('approve_all') }}"
  onsubmit="return confirm('Send {{ remaining }} REAL cold emails now?')">
  <button class="btn btn-primary">Approve &amp; send all {{ remaining }}</button>
</form>
{% endif %}
</div>

{% if not queue and not needs_email %}
<div class="card"><p class="muted">Nothing waiting. Run a search on the
dashboard, or turn on automatic searching in Setup and leads will show up here
by themselves.</p></div>
{% endif %}

{% for item in queue %}
<div class="card">
  <h2 style="margin-bottom:2px">{{ item.lead['name'] }}</h2>
  <div class="muted">{{ item.lead['category'] or '' }}{% if item.lead['address'] %}
    · {{ item.lead['address'] }}{% endif %}{% if item.lead['phone'] %}
    · {{ item.lead['phone'] }}{% endif %}</div>
  {% if item.lead['site_status'] and item.lead['site_status'] != 'none' %}
  <div class="muted" style="margin-top:4px">Why they're a lead:
    <b>{{ item.lead['site_status']|reason }}</b>
    {% if item.lead['social_url'] %}—
      <a href="{{ item.lead['social_url'] }}" target="_blank"
         rel="noopener noreferrer">{{ item.lead['social_url'][:70] }}</a>{% endif %}
    {% if item.lead['site_note'] %}<span style="opacity:.7">({{ item.lead['site_note'] }})</span>{% endif %}
  </div>
  {% endif %}
  <div class="muted" style="margin:6px 0"><b>To:</b> {{ item.lead['email'] }}
  {% if item.lead['email_source'] %}
    <span style="color:var(--warn)">· JARVIS found this, you haven't checked it</span>
    <div style="font-size:12px;margin-top:2px">from
      <a href="{{ item.lead['email_source'] }}" target="_blank"
         rel="noopener noreferrer">{{ item.lead['email_source'][:90] }}</a></div>
  {% endif %}</div>
  {% if item.rendered.ok %}
  <div class="emailbox">
    <div class="subj">{{ item.rendered.subject }}</div>
    <div class="body">{{ item.rendered.body }}</div>
  </div>
  {% else %}<p style="color:var(--bad)">{{ item.rendered.error }}</p>{% endif %}
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <form class="inline" method="post"
      action="{{ url_for('approve_lead', lead_id=item.lead['id']) }}">
      <button class="btn btn-primary">Approve &amp; send</button></form>
    <form class="inline" method="post"
      action="{{ url_for('not_interested', lead_id=item.lead['id']) }}">
      <button class="btn">Skip this one</button></form>
  </div>
</div>
{% endfor %}

{% if needs_email %}
<div class="card">
<h2>Need an email address ({{ needs_email|length }})</h2>
<p class="muted">Google doesn't publish business emails. The Researcher hunts
for them online — accept what it finds, or look one up yourself (Facebook, Yelp,
a quick call) and paste it in.</p>
<form method="post" action="{{ url_for('run_research') }}" style="margin-bottom:12px">
  <button class="btn">🕵️ Send the Researcher after these</button></form>
<div class="tablewrap"><table class="stack"><tbody>
{% for l in needs_email %}
<tr>
  <td><b>{{ l['name'] }}</b><div class="muted">{{ l['category'] or '' }}
      {% if l['phone'] %}· {{ l['phone'] }}{% endif %}</div></td>
  <td style="min-width:210px">
    {% if l['suggested_email'] %}
    <div class="note info" style="padding:9px 11px;margin-bottom:8px">
      <div class="k">Researcher found</div>
      <div style="font-weight:600;word-break:break-all">{{ l['suggested_email'] }}</div>
      {% if l['suggested_email_note'] %}<div class="muted">{{ l['suggested_email_note'] }}</div>{% endif %}
      {% if l['suggested_email_source'] %}<div class="muted">
        <a href="{{ l['suggested_email_source'] }}" target="_blank"
          rel="noopener noreferrer">where it found it ↗</a></div>{% endif %}
      <div style="display:flex;gap:6px;margin-top:7px">
        <form class="inline" method="post"
          action="{{ url_for('accept_email', lead_id=l['id']) }}">
          <button class="btn btn-sm btn-primary">Use this</button></form>
        <form class="inline" method="post"
          action="{{ url_for('reject_email', lead_id=l['id']) }}">
          <button class="btn btn-sm">No</button></form>
      </div>
    </div>
    {% endif %}
    <form method="post" action="{{ url_for('set_email', lead_id=l['id']) }}"
      style="display:flex;gap:6px">
      <input type="text" name="email" placeholder="email@business.com"
        inputmode="email" autocapitalize="off">
      <button class="btn btn-sm">Save</button></form></td>
</tr>
{% endfor %}
</tbody></table></div>
</div>
{% endif %}
{% endblock %}
"""


@app.get("/approve")
def approve_queue():
    db = STATE.db
    # Show only the leads at or above a chosen bar. Same three widths the
    # crawler uses, applied to what you're looking at rather than what gets
    # collected — so you can narrow the queue without throwing leads away.
    want = request.args.get("only", "")
    keep = core.QUALITY_LEVELS.get(want)
    leads = db.leads_awaiting_approval()
    if keep:
        leads = [l for l in leads if (l["site_status"] or core.SITE_NONE) in keep]
    queue = [{"lead": lead, "rendered": STATE.agent.render_outreach(lead)}
             for lead in leads]
    cap = int(STATE.config.get("daily_send_cap", 20) or 0)
    sent_today = db.sends_today()
    remaining = len(queue)
    if cap:
        remaining = max(0, min(remaining, cap - sent_today))
    counts = {}
    for lead in db.leads_awaiting_approval():
        key = lead["site_status"] or core.SITE_NONE
        counts[key] = counts.get(key, 0) + 1
    return _render(APPROVE_PAGE, queue=queue, needs_email=db.leads_needing_email(),
                   cap=cap, sent_today=sent_today, remaining=remaining,
                   only=want if keep else "", counts=counts,
                   reasons=core.SITE_REASON)


@app.post("/action/approve/<int:lead_id>")
def approve_lead(lead_id):
    _flash_result(STATE.agent.send_outreach(lead_id), "Cold email sent.")
    return redirect(url_for("approve_queue"))


@app.post("/action/approve_all")
def approve_all():
    sent, failed, last_error = 0, 0, None
    for lead in STATE.db.leads_awaiting_approval():
        result = STATE.agent.send_outreach(lead["id"])
        if result.get("ok"):
            sent += 1
        else:
            failed += 1
            last_error = result.get("error")
            if "Daily limit" in (last_error or ""):
                break  # stop at the cap rather than failing one by one
    if sent:
        flash(f"Sent {sent} cold email{'' if sent == 1 else 's'}."
              + (f" {failed} not sent: {last_error}" if failed else ""), "ok")
    else:
        flash(last_error or "Nothing to send.", "err")
    return redirect(url_for("approve_queue"))


@app.post("/action/run_searches")
def run_searches():
    def work():
        r = STATE.agent.run_saved_searches(force=True)
        if r.get("skipped"):
            return f"Nothing to do — {r['skipped']}. Add searches in Setup."
        return r.get("summary", "Search finished.")

    if _start_job("searches", "running your saved searches", work):
        flash("Running your saved searches now — this page updates itself.", "ok")
    else:
        flash(f"JARVIS is busy — {JOB['label']}.", "err")
    return redirect(url_for("approve_queue"))



TEAM_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Your team</h1>
<p class="muted" style="margin-top:-6px">Eight specialists. Each owns one job and
hands off to the next. Two of them ask you before acting — everything else runs
on its own.</p>
<p class="muted" style="margin:8px 0 0"><b>WAITING</b> means keyed up with
nothing in front of it. That's normal, and it's what most of these look like
until a lead reaches them — they only wake when there's something on their
desk. <b>WORKING</b> means there is.</p>
</div>

{% for m in team %}
<div class="card" style="border-left:4px solid {{ m.color }}">
  <div style="display:flex;justify-content:space-between;align-items:baseline;
    gap:10px;flex-wrap:wrap">
    <h2 style="margin:0">{{ m.icon }} {{ m.name }}</h2>
    <span class="badge" style="background:{{ m.chip_bg }};color:{{ m.chip_fg }}">
      {{ m.status }}</span>
  </div>
  <p style="margin:6px 0 4px">{{ m.job }}</p>
  <div class="muted">Runs on: {{ m.powered }}</div>
  {% if m.gate %}<div class="muted" style="color:var(--warn);margin-top:4px">
    ⚑ {{ m.gate }}</div>{% endif %}
  <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:10px">
    {% for label, value in m.stats %}
    <div><div class="muted" style="font-size:11px;text-transform:uppercase;
      letter-spacing:.05em">{{ label }}</div>
      <b style="font-size:19px">{{ value }}</b></div>
    {% endfor %}
  </div>
  {% if m.last %}<div class="muted" style="margin-top:8px">
    Last: {{ m.last }}</div>{% endif %}
</div>
{% endfor %}
{% endblock %}
"""


# The eight specialists laid out as rooms, in the order work moves through
# them. Each row is (key, storey, icon, name, what the number in the room means).
HOUSE_ROOMS = [
    ("scout",       3, "🔭", "Scout",      "leads found"),
    ("researcher",  3, "🕵️", "Researcher", "hunting an email"),
    ("copywriter",  3, "✍️", "Copywriter", "drafts written"),
    ("triage",      1, "📬", "Triage",     "waiting on a reply"),
    ("designer",    1, "🎨", "Designer",   "being designed"),
    ("deployer",    1, "🚀", "Deployer",   "preview out"),
    ("biller",      0, "💳", "Biller",     "awaiting payment"),
    ("delivery",    0, "📦", "Delivery",   "paid, shipping"),
]


def _house_state() -> dict:
    """Where every lead is standing right now, room by room.

    The Team page answers "what does each specialist do"; this answers "what is
    each of them holding at this second", which is what makes the house move.
    """
    db, cfg = STATE.db, STATE.config
    leads = db.all_leads()
    ev = db.event_counts()
    auto = bool(cfg.get("autopilot_enabled"))

    def at(*stages):
        return sum(1 for l in leads if l["stage"] in stages)

    def who(*stages, rows=None):
        src = rows if rows is not None else [l for l in leads
                                             if l["stage"] in stages]
        return [l["name"] for l in src[:8]]

    waiting = len(db.leads_awaiting_approval())
    needs_email = len(db.leads_needing_email())

    def room(key, ready, on, count, missing="", names=()):
        return {"key": key, "count": count, "names": list(names),
                "state": "nokey" if not ready else ("on" if on else "standby"),
                "note": missing if not ready else ""}

    anth = bool(cfg.get("anthropic_api_key"))
    rooms = [
        room("scout", bool(cfg.get("google_places_api_key")),
             bool(cfg.get("auto_search_enabled")), len(leads),
             "Needs your Google Places key", who(rows=leads)),
        room("researcher", anth, bool(cfg.get("auto_research_enabled")),
             needs_email, "Needs your Anthropic key",
             who(rows=db.leads_needing_email())),
        room("copywriter", bool(cfg.get("inkbox_api_key")), True, waiting,
             "Needs your Inkbox key", who(rows=db.leads_awaiting_approval())),
        room("triage", anth, auto, at(core.STAGE_CONTACTED),
             "Needs your Anthropic key", who(core.STAGE_CONTACTED)),
        room("designer", anth, auto, at(core.STAGE_BUILDING_PREVIEW),
             "Needs your Anthropic key", who(core.STAGE_BUILDING_PREVIEW)),
        room("deployer", bool(cfg.get("netlify_api_key")), auto,
             at(core.STAGE_PREVIEW_SENT), "Needs your Netlify token",
             who(core.STAGE_PREVIEW_SENT)),
        room("biller", bool(cfg.get("stripe_secret_key")), auto,
             at(core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT),
             "Needs your Stripe key",
             who(core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT)),
        room("delivery", bool(cfg.get("netlify_api_key")) and
             bool(cfg.get("stripe_secret_key")), auto,
             at(core.STAGE_PAID, core.STAGE_DEPLOYING_FINAL),
             "Needs your Netlify and Stripe keys",
             who(core.STAGE_PAID, core.STAGE_DEPLOYING_FINAL)),
    ]

    latest = db.recent_events(1)
    return {
        "rooms": rooms,
        "you": {"waiting": waiting, "attention": len(db.attention_events())},
        "vault": {"collected": db.revenue_cents() // 100,
                  "pending": db.pending_cents() // 100,
                  "delivered": at(core.STAGE_DELIVERED)},
        "autopilot": auto,
        "emails_today": db.sends_today(),
        "replies": ev.get("reply_received", 0),
        "last_event": latest[0]["id"] if latest else 0,
    }


def _team_roster():
    """The pipeline described as the specialists who actually do each job."""
    db, cfg = STATE.db, STATE.config
    ev = db.event_counts()
    stages = {}
    for lead in db.all_leads():
        stages[lead["stage"]] = stages.get(lead["stage"], 0) + 1

    def last_detail(*kinds):
        for e in db.recent_events(120):
            if e["kind"] in kinds:
                return (e["detail"] or "")[:110]
        return ""

    def state(ready: bool, on: bool = True, missing: str = "", queue: int = 0):
        """queue is what's in front of this specialist right now. With a key
        and nothing to do, the honest word is WAITING — ON DUTY reads like
        it's mid-task, which is what made the first run look broken."""
        if not ready:
            return "NEEDS KEY", "rgba(255,122,94,.16)", "#ffab94", missing
        if not on:
            return "STANDBY", "rgba(190,170,255,.08)", "#9689ab", ""
        if not queue:
            return "WAITING", "rgba(129,140,248,.16)", "#a5b4fc", ""
        return "WORKING", "rgba(110,231,183,.16)", "#8ff0cb", ""

    auto = bool(cfg.get("autopilot_enabled"))
    members = [
        dict(icon="🔭", name="Scout", color="#6366f1",
             job="Searches Google Places for local businesses that have no "
                 "website, and adds them as leads.",
             powered="Google Places API",
             stats=[("Searches run", ev.get("find_leads", 0)),
                    ("Leads found", len(db.all_leads()))],
             last=last_detail("find_leads", "auto_search"),
             st=state(bool(cfg.get("google_places_api_key")),
                      bool(cfg.get("auto_search_enabled")),
                      "Add your Google Places key in Setup",
                      queue=1 if cfg.get("saved_searches", "").strip() else 0)),
        dict(icon="🕵️", name="Researcher", color="#0ea5e9",
             job="Searches the web for the contact email of businesses that "
                 "don't have one — Facebook, Yelp, directories.",
             powered="Claude with web search",
             stats=[("Emails found", ev.get("email_suggested", 0)),
                    ("Came up empty", ev.get("email_not_found", 0)),
                    ("Still no email", len(db.leads_needing_email()))],
             last=last_detail("email_suggested", "email_not_found"),
             gate="Suggests only — you accept each address",
             st=state(bool(cfg.get("anthropic_api_key")),
                      bool(cfg.get("auto_research_enabled")),
                      "Add your Anthropic key in Setup",
                      queue=len(db.leads_needing_email()))),
        dict(icon="✍️", name="Copywriter", color="#8b5cf6",
             job="Writes each cold email from your template, personalised with "
                 "the business name and your details.",
             powered="Your template in Setup",
             stats=[("Waiting for you", len(db.leads_awaiting_approval())),
                    ("Approved & sent", ev.get("outreach_sent", 0)),
                    ("Sent today", db.sends_today())],
             last=last_detail("outreach_sent"),
             gate="Never sends without your approval",
             st=state(bool(cfg.get("inkbox_api_key")), True,
                      "Add your Inkbox key in Setup",
                      queue=len(db.leads_awaiting_approval()))),
        dict(icon="📬", name="Triage", color="#f59e0b",
             job="Reads every reply and works out whether they're interested, "
                 "not interested, or asking a question.",
             powered="Claude",
             stats=[("Replies read", ev.get("reply_received", 0)),
                    ("Passed to you", ev.get("reply_unclear", 0)),
                    ("Opted out", ev.get("unsubscribed", 0))],
             last=last_detail("reply_received"),
             st=state(bool(cfg.get("anthropic_api_key")), auto,
                      "Add your Anthropic key in Setup",
                      queue=stages.get(core.STAGE_CONTACTED, 0))),
        dict(icon="🎨", name="Designer", color="#ec4899",
             job="Designs a complete one-page website for the business, "
                 "matched to what they do.",
             powered="Claude",
             stats=[("Sites designed", ev.get("site_generated", 0))],
             last=last_detail("site_generated"),
             st=state(bool(cfg.get("anthropic_api_key")), auto,
                      "Add your Anthropic key in Setup",
                      queue=stages.get(core.STAGE_BUILDING_PREVIEW, 0))),
        dict(icon="🚀", name="Deployer", color="#14b8a6",
             job="Publishes the watermarked preview to the web and emails the "
                 "link to the lead.",
             powered="Netlify",
             stats=[("Previews live", ev.get("preview_deployed", 0)),
                    ("Links emailed", ev.get("preview_emailed", 0))],
             last=last_detail("preview_deployed"),
             st=state(bool(cfg.get("netlify_api_key")), auto,
                      "Add your Netlify token in Setup",
                      queue=stages.get(core.STAGE_PREVIEW_SENT, 0))),
        dict(icon="💳", name="Biller", color="#22c55e",
             job="Creates the payment link, emails it, and watches Stripe "
                 "around the clock until the money clears.",
             powered="Stripe",
             stats=[("Links sent", ev.get("payment_link_emailed", 0)),
                    ("Payments confirmed", ev.get("payment_confirmed", 0)),
                    ("Awaiting payment",
                     stages.get(core.STAGE_PAYMENT_LINK_SENT, 0))],
             last=last_detail("payment_confirmed", "payment_link_emailed"),
             st=state(bool(cfg.get("stripe_secret_key")), auto,
                      "Add your Stripe key in Setup",
                      queue=stages.get(core.STAGE_PAYMENT_LINK_SENT, 0))),
        dict(icon="📦", name="Delivery", color="#0284c7",
             job="Once Stripe confirms payment, strips the watermark, puts the "
                 "real site live, and emails the customer.",
             powered="Netlify + Stripe check",
             stats=[("Sites delivered", ev.get("delivered", 0)),
                    ("Revenue", "$" + f"{db.revenue_cents() // 100:,}")],
             last=last_detail("delivered"),
             gate="Blocked until Stripe confirms payment",
             st=state(bool(cfg.get("netlify_api_key")
                           and cfg.get("stripe_secret_key")), auto,
                      "Add your Netlify and Stripe keys in Setup",
                      queue=stages.get(core.STAGE_PAID, 0))),
    ]
    for m in members:
        status, bg, fg, missing = m.pop("st")
        m["status"], m["chip_bg"], m["chip_fg"] = status, bg, fg
        m.setdefault("gate", "")
        if missing:
            m["gate"] = missing
    return members


ROOM_TINT = {
    "scout": "rgba(244,114,182,.48)",    "researcher": "rgba(129,140,248,.45)",
    "copywriter": "rgba(192,132,252,.45)", "triage": "rgba(252,211,77,.42)",
    "designer": "rgba(232,121,249,.42)", "deployer": "rgba(103,232,249,.40)",
    "biller": "rgba(110,231,183,.42)",   "delivery": "rgba(167,139,250,.45)",
}


@app.get("/calls")
def calls_page():
    cfg = STATE.config
    leads = STATE.db.leads_to_call()
    return _render(CALLS, leads=[
        {"lead": l,
         "tel": re.sub(r"[^0-9+]", "", l["phone"] or ""),
         "opener": core.call_opener(dict(l), cfg)} for l in leads])


@app.post("/action/call_email/<int:lead_id>")
def call_got_email(lead_id):
    """They gave you an address on the phone — put them in the normal queue."""
    email = (request.form.get("email") or "").strip()
    STATE.db.update_lead(lead_id, last_called_at=core._now())
    result = STATE.agent.set_email(lead_id, email)
    if result.get("ok"):
        lead = STATE.db.get_lead(lead_id)
        STATE.db.log(lead_id, "call_logged",
                     f"Called {lead['name']} — they gave {email}")
        flash(f"Saved. {lead['name']} is on the Approve page — the email still "
              "needs your OK before it sends.", "ok")
    else:
        flash(result.get("error", "That didn't look like an email address."), "err")
    return redirect(url_for("calls_page"))


@app.post("/action/call_logged/<int:lead_id>")
def call_logged(lead_id):
    lead = STATE.db.get_lead(lead_id)
    if lead is None:
        abort(404)
    STATE.db.update_lead(lead_id, last_called_at=core._now())
    STATE.db.log(lead_id, "call_logged", f"Called {lead['name']} — no answer")
    flash(f"Logged. {lead['name']} drops to the bottom of the list.", "ok")
    return redirect(url_for("calls_page"))


@app.post("/action/call_pass/<int:lead_id>")
def call_pass(lead_id):
    lead = STATE.db.get_lead(lead_id)
    if lead is None:
        abort(404)
    STATE.db.update_lead(lead_id, last_called_at=core._now(), do_not_contact=1)
    STATE.db.claim(lead_id, [core.STAGE_FOUND], core.STAGE_NOT_INTERESTED)
    STATE.db.log(lead_id, "call_logged",
                 f"Called {lead['name']} — not interested, won't contact again")
    flash(f"{lead['name']} won't be contacted again.", "ok")
    return redirect(url_for("calls_page"))


@app.get("/house")
def house_page():
    rooms = [{"key": k, "storey": storey, "icon": icon, "name": name,
              "doing": doing, "tint": ROOM_TINT[k]}
             for k, storey, icon, name, doing in HOUSE_ROOMS]
    return _render(HOUSE, rooms=rooms, initial=_house_state())


@app.get("/house/data")
def house_data():
    return jsonify(_house_state())


@app.get("/team")
def team_page():
    return _render(TEAM_PAGE, team=_team_roster())


@app.post("/action/accept_email/<int:lead_id>")
def accept_email(lead_id):
    _flash_result(STATE.agent.accept_suggested_email(lead_id), "Email saved.")
    return redirect(url_for("approve_queue"))


@app.post("/action/reject_email/<int:lead_id>")
def reject_email(lead_id):
    STATE.agent.reject_suggested_email(lead_id)
    return redirect(url_for("approve_queue"))


@app.post("/action/run_research")
def run_research():
    """Looking up an address is a web search per business — a minute for a
    batch of them. It runs in the background; the page says so and comes back
    on its own."""
    def work():
        r = STATE.agent.research_missing_emails(force=True, limit=10)
        return ("Looked up %d business%s and found %d email address%s."
                % (r.get("researched", 0),
                   "" if r.get("researched") == 1 else "es",
                   r.get("found", 0), "" if r.get("found") == 1 else "es"))

    if _start_job("research", "looking up email addresses", work):
        flash("JARVIS is looking up their email addresses now. It takes a "
              "minute or two — this page updates itself.", "ok")
    else:
        flash(f"JARVIS is busy — {JOB['label']}.", "err")
    return redirect(url_for("approve_queue"))



UPDATES_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Updates</h1>
{% if cloud_mode %}
<p>This copy runs in the cloud, and your host redeploys it automatically
whenever the code changes — <b>there's nothing for you to do</b>. Updates just
appear.</p>
{% else %}
  {% if not info.ok %}
    <p style="color:var(--bad)">{{ info.error }}</p>
    <form method="post" action="{{ url_for('check_update') }}">
      <button class="btn">Try again</button></form>
  {% elif info.available %}
    <div class="note info" style="margin-bottom:14px">
      <div style="font-weight:600;font-size:16px">An update is ready</div>
      <div class="muted" style="margin-top:4px">{{ info.message }}</div>
      <div class="muted">Released {{ info.date }} · version {{ info.short }}</div>
    </div>
    <form method="post" action="{{ url_for('do_update') }}">
      <button class="btn btn-primary">Install update</button></form>
    <p class="muted" style="margin-bottom:0">Takes a few seconds. Your leads,
    settings and API keys are untouched — they live outside the app.</p>
  {% else %}
    <p>✅ <b>You're up to date.</b> <span class="muted">Version {{ info.short }},
    released {{ info.date }}.</span></p>
    <form method="post" action="{{ url_for('check_update') }}">
      <button class="btn">Check again</button></form>
  {% endif %}
  {% if restart_needed %}
    <div class="note warn" style="margin-top:14px">
      <div style="font-weight:600">Update installed — restart to use it</div>
      <form method="post" action="{{ url_for('do_restart') }}" style="margin-top:8px">
        <button class="btn btn-primary">Restart Solo Studio</button></form>
      <div class="muted" style="margin-top:6px">The page will go blank for a few
      seconds, then come back on its own.</div>
    </div>
  {% endif %}
{% endif %}
</div>
{% endblock %}
"""


def _spend_so_far() -> dict:
    """What the app has actually spent this month, so it isn't a guess.

    Roughly 3.5 cents a paid lookup: two web searches at $10 per thousand,
    plus a small model's tokens on what they return.
    """
    try:
        lookups = STATE.agent.paid_lookups_this_month()
        google = STATE.agent.google_calls_this_month()
    except Exception:
        lookups = google = 0
    cfg = STATE.config
    return {
        "lookups": lookups,
        "lookup_cap": core.setting_int(cfg, "monthly_lookup_cap", core.LOOKUP_CAP),
        "lookup_dollars": f"{lookups * 0.035:.2f}",
        "model": (cfg.get("research_model") or core.RESEARCH_MODEL),
        "google": google,
        "google_cap": core.setting_int(cfg, "monthly_google_cap",
                                       core.GOOGLE_CALL_CAP),
    }


def _crawl_progress() -> dict:
    """Which city JARVIS is on, and how far through the country."""
    db = STATE.db
    try:
        cities = json.loads(db.get_kv("crawl_cities") or "[]")
        at = int(db.get_kv("crawl_city") or 0)
    except (TypeError, ValueError):
        return {}
    if not cities:
        return {}
    at = min(at, len(cities) - 1)
    return {"at": at + 1, "of": len(cities), "city": cities[at]}


def current_findings() -> list[dict]:
    """What JARVIS has noticed right now, including the things only the app
    itself knows — an update sitting on disk, for one."""
    extra = []
    crawl = _crawl_progress()
    if crawl and STATE.config.get("auto_search_enabled"):
        extra.append(core.finding(
            "crawling", core.WATCH,
            "JARVIS is working through %s" % crawl["city"],
            "City %d of %d — he goes city by city, state by state, on his own "
            "while the app is open." % (crawl["at"], crawl["of"]),
            "/map", "See the map"))
    if not CLOUD_MODE and _restart_pending():
        extra.append(core.finding(
            "restart-pending", core.WATCH, "An update is installed but not running",
            "Restart to start using it.", "/updates", "Restart"))
    try:
        spent = STATE.agent.google_calls_this_month()
    except Exception:
        spent = None
    return core.checkup(STATE.db, STATE.config,
                        [(k["name"], k["field"]) for k in KEY_FIELDS
                         if not k.get("optional")],
                        extra, spent=spent)


def _restart_pending() -> bool:
    """True when installed code is newer than what this process is running."""
    return bool(core.installed_version().get("sha")
                and core.installed_version().get("sha") != RUNNING_SHA)


@app.get("/updates")
def updates_page():
    info = {"ok": True, "available": False, "short": "—", "date": ""}
    if not CLOUD_MODE:
        info = core.check_for_update()
    return _render(UPDATES_PAGE, info=info, restart_needed=_restart_pending())


@app.post("/action/check_update")
def check_update():
    return redirect(url_for("updates_page"))


@app.post("/action/update")
def do_update():
    result = core.apply_update()
    if result.get("ok"):
        flash(f"Update {result['short']} installed. Restart to start using it.", "ok")
    else:
        flash(result.get("error", "Update failed."), "err")
    return redirect(url_for("updates_page"))


def _compiles(directory: str) -> bool:
    """Is there a complete, startable copy of the app in here?"""
    files = [os.path.join(directory, n)
             for n in ("dashboard_app.py", "solo_studio_agent.py")]
    if not all(os.path.exists(f) for f in files):
        return False
    try:
        for f in files:
            py_compile.compile(f, doraise=True)
    except (py_compile.PyCompileError, OSError, ValueError):
        return False
    return True


def _code_to_run() -> str:
    """Which copy of the app a restart should come back on.

    A downloaded update if there is one, otherwise the copy we are running
    now — and only after checking it actually compiles, the same guard the
    launcher applies. Returns "" when nothing on disk starts, in which case
    there must be no restart at all: the code already running is then the last
    working copy in existence and quitting would lose it.
    """
    candidates = []
    try:
        candidates.append(core.updates_dir())
    except OSError:
        pass
    mine = os.path.dirname(os.path.abspath(__file__))
    if mine not in candidates:
        candidates.append(mine)
    for d in candidates:
        if _compiles(d):
            return d
    return ""


MAX_FD = 4096            # plenty: this process opens a few dozen at most


def _drop_open_files() -> None:
    """Make sure nothing we hold open survives into the process that replaces us.

    Python closes its files on exec, but the web server deliberately does not:
    it marks its listening socket inheritable so a reloader can pass the port
    along. Left that way the new process finds its own port occupied and dies
    on startup. Hand nothing over but the console.
    """
    os.environ.pop("WERKZEUG_SERVER_FD", None)   # no fd to hand over any more
    for fd in range(3, MAX_FD):
        try:
            os.set_inheritable(fd, False)
        except OSError:
            pass


def _relaunch_self() -> None:
    """Replace this process with a fresh one, no launcher required.

    Raises OSError if the exec fails, in which case we are still the old
    process, still running, and still serving the dashboard.
    """
    directory = _code_to_run()
    if not directory:
        raise OSError("nothing on disk compiles — staying on the copy already "
                      "running rather than quitting into a broken one")
    target = os.path.join(directory, "dashboard_app.py")
    _drop_open_files()
    os.execv(sys.executable,
             [sys.executable, target, "--port", str(RUN_PORT)])


@app.post("/action/restart")
def do_restart():
    """Come back on the newly installed code.

    Under a launcher that announces itself, exiting with its restart code is
    best: it reinstalls anything new an update needs on the way back. Every
    other case — run by hand, or a launcher too old to say so — we relaunch
    ourselves, so the button always works instead of telling the user to go
    quit the app.
    """
    back = request.form.get("back") or url_for("updates_page")
    if CLOUD_MODE:
        flash("The cloud version restarts itself on deploy.", "err")
        return redirect(back)

    def bye():
        time.sleep(0.7)          # let this response reach the browser first
        if LAUNCHER_RERUNS_US:
            os._exit(core.RESTART_EXIT_CODE)
        else:
            try:
                _relaunch_self()
            except OSError as e:
                # Still alive, still on the old code. The Updates page goes on
                # saying a restart is needed, which is the truth.
                print(f"Couldn't relaunch: {e}", file=sys.stderr)

    threading.Thread(target=bye, daemon=True).start()
    return render_template_string(RESTARTING_PAGE, pwa_meta=PWA_META)


RESTARTING_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Restarting…</title>{{ pwa_meta|safe }}
<style>body{background:#0b0912;color:#f0ecf7;font:16px -apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;
text-align:center}h1{font-size:19px;letter-spacing:.2em;color:#dbd9ff}
.dot{animation:b 1.2s infinite}@keyframes b{50%{opacity:.25}}</style></head>
<body><div><h1>RESTARTING<span class="dot">…</span></h1>
<p class="muted">This page comes back on its own.</p></div>
<script>
(function retry() {
  setTimeout(function () {
    fetch('/health', { cache: 'no-store' })
      .then(function (r) { if (r.ok) location.href = '/'; else retry(); })
      .catch(retry);
  }, 1500);
})();
</script>
</body></html>"""


@app.get("/activity")
def activity():
    return _render(ACTIVITY, events=STATE.db.recent_events(200))


@app.get("/lead/<int:lead_id>")
def lead_page(lead_id):
    lead = STATE.db.get_lead(lead_id)
    if lead is None:
        abort(404)
    events = [e for e in STATE.db.recent_events(500) if e["lead_id"] == lead_id]
    return _render(LEAD_PAGE, lead=lead, events=events)


@app.get("/lead/<int:lead_id>/site.html")
def lead_site_html(lead_id):
    lead = STATE.db.get_lead(lead_id)
    if lead is None or not lead["site_html"]:
        abort(404)
    return lead["site_html"], 200, {"Content-Type": "text/html; charset=utf-8"}


def _flash_result(result: dict, ok_msg: str):
    if result.get("ok"):
        flash(ok_msg, "ok")
    else:
        flash(result.get("error", "Something went wrong."), "err")


# Anything JARVIS does that takes real time — hunting an area, looking up a
# batch of email addresses, a full pass over the pipeline — runs here rather
# than inside a request. A dozen searches or five web lookups is a minute or
# two, and a page that hangs that long reads as a broken app. One job at a
# time: they all spend the same API budget, and racing them helps nobody.
JOB = {"running": "", "label": "", "summary": ""}
_job_lock = threading.Lock()


def _start_job(name: str, label: str, work) -> bool:
    """Put JARVIS to work in the background. False if he is already busy."""
    with _job_lock:
        if JOB["running"]:
            return False
        JOB.update(running=name, label=label, summary="")

    def go():
        try:
            summary = work()
        except Exception as e:
            summary = core.explain(e, 300)
        with _job_lock:
            JOB.update(running="", summary=summary or "Done.")

    threading.Thread(target=go, daemon=True, name="solo-studio-" + name).start()
    return True


def _start_hunt(area: str) -> bool:
    return _start_job("hunt", f"out hunting for leads near {area}",
                      lambda: STATE.agent.hunt(area)["summary"])


def _names_a_trade(query: str) -> bool:
    """Does this read as "what, where" rather than just "where"?

    A bare place name is the trap: searching a business directory for "Los
    Angeles" returns the city and the biggest firms in it, every one of which
    has a website, and the empty result looks like the app is broken.
    """
    low = " %s " % query.lower()
    if " in " in low:
        return True
    return any(" %s " % t.lower() in low or low.strip() == t.lower()
               for t in core.DEFAULT_TRADES)


@app.post("/action/find_leads")
def find_leads():
    query = (request.form.get("query") or "").strip()
    if not query:
        flash("Type a search first.", "err")
        return redirect(url_for("dashboard"))

    # Just a place? Don't run a search that cannot work — go hunting.
    if not _names_a_trade(query):
        if _start_hunt(query):
            flash(f"JARVIS is out hunting near {query}. This takes a minute — "
                  "the page updates itself when he's back.", "ok")
        else:
            flash(f"JARVIS is busy — {JOB['label']}.", "err")
        return redirect(url_for("dashboard"))

    try:
        r = STATE.agent.find_leads(query)
    except Exception as e:
        flash(core.explain(e, 300), "err")
        return redirect(url_for("dashboard"))

    if r["added"]:
        flash(core.describe_search(r), "ok")
        return redirect(url_for("dashboard"))

    # Nothing new. Rather than leave them with an empty result, go and look
    # properly: other trades, the towns around it, until something turns up.
    area = query.split(" in ", 1)[1].strip() if " in " in query else query
    if _start_hunt(area):
        flash(core.describe_search(r) + f" JARVIS is now hunting near {area} "
              "on his own — the page updates itself when he's back.", "ok")
    else:
        flash(core.describe_search(r), "err")
    return redirect(url_for("dashboard"))


@app.post("/action/send_outreach/<int:lead_id>")
def send_outreach(lead_id):
    _flash_result(STATE.agent.send_outreach(lead_id), "Cold email sent.")
    return redirect(url_for("dashboard"))


@app.post("/action/advance/<int:lead_id>")
def advance(lead_id):
    r = STATE.agent.manual_advance(lead_id)
    if r.get("ok"):
        lead = STATE.db.get_lead(lead_id)
        if lead and lead["error"]:
            flash(f"Step started but hit a problem (will keep retrying): "
                  f"{lead['error']}", "err")
        else:
            flash("Done — see the lead's history below.", "ok")
    else:
        flash(r.get("error", "Something went wrong."), "err")
    return redirect(url_for("lead_page", lead_id=lead_id))


@app.post("/action/not_interested/<int:lead_id>")
def not_interested(lead_id):
    _flash_result(STATE.agent.manual_not_interested(lead_id), "Marked not interested.")
    return redirect(url_for("dashboard"))


@app.post("/action/retry/<int:lead_id>")
def retry_lead(lead_id):
    _flash_result(STATE.agent.retry_from_error(lead_id), "Retried.")
    return redirect(url_for("lead_page", lead_id=lead_id))


@app.post("/action/new_payment_link/<int:lead_id>")
def new_payment_link(lead_id):
    _flash_result(STATE.agent.new_payment_link(lead_id), "New payment link sent.")
    return redirect(url_for("lead_page", lead_id=lead_id))


@app.post("/action/set_email/<int:lead_id>")
def set_email(lead_id):
    _flash_result(STATE.agent.set_email(lead_id, request.form.get("email", "")),
                  "Email saved.")
    return redirect(url_for("dashboard"))


@app.post("/action/check_now")
def check_now():
    try:
        r1 = STATE.agent.process_replies()
        r2 = STATE.agent.poll_payments()
        STATE.agent.tick_transients()
        flash(f"Checked. Replies handled: {r1.get('handled', 0)}; payments checked: "
              f"{r2.get('checked', 0)}; newly paid: {r2.get('newly_paid', 0)}.", "ok")
    except Exception as e:
        flash(str(e), "err")
    return redirect(url_for("dashboard"))


@app.post("/action/toggle_autopilot")
def toggle_autopilot():
    cfg = core.load_config()
    cfg["autopilot_enabled"] = not cfg.get("autopilot_enabled")
    core.save_config(cfg)
    STATE.reload()
    flash("Autopilot is now " + ("ON — replies and payments are handled "
          "automatically while this app is open." if cfg["autopilot_enabled"]
          else "OFF."), "ok")
    return redirect(url_for("dashboard"))


@app.post("/action/resolve_event/<int:event_id>")
def resolve_event(event_id):
    STATE.db.resolve_event(event_id)
    return redirect(url_for("dashboard"))


# Each key, with click-by-click directions to go and get it. Order follows the
# pipeline: find the business, email it, design the site, host it, get paid.
def assistant_snapshot() -> str:
    """A plain-text picture of the owner's pipeline right now, for the helper.

    Bounded on purpose — a handful of lines per section, so the brief stays
    small however many leads pile up.
    """
    db, cfg = STATE.db, STATE.config
    out = [f"LIVE SNAPSHOT (taken {core._now()})"]

    # The watchman's list first: it is the answer to "what's wrong?", and it
    # is the same list the owner is looking at on screen.
    found = current_findings()
    if found:
        out.append("\nWHAT NEEDS THEM RIGHT NOW (worst first):")
        for f in found:
            out.append("  [%s] %s — %s (%s)"
                       % (f["level"].upper(), f["title"], f["detail"], f["where"]))
    else:
        out.append("\nNothing is broken and nothing is waiting on them.")

    missing = [k["name"] for k in KEY_FIELDS
               if not cfg.get(k["field"]) and not k.get("optional")]
    out.append("Setup: " + ("every API key is saved." if not missing else
               "still missing " + ", ".join(missing) + "."))
    for field, label in (("your_name", "name"), ("mailing_address", "mailing address")):
        if not cfg.get(field):
            out.append(f"Their {label} is not filled in on Setup yet.")
    out.append("Autopilot is %s. Price per site: $%s. Daily cold-email cap: %s."
               % ("ON" if cfg.get("autopilot_enabled") else "OFF",
                  core.fmt_price(cfg.get("site_price_usd", 500)),
                  cfg.get("daily_send_cap", 20)))
    out.append("Sent today: %d cold emails." % db.sends_today())

    leads = db.all_leads()
    if not leads:
        out.append("\nNo leads yet — the pipeline is empty.")
    else:
        stages = {}
        for lead in leads:
            stages[lead["stage"]] = stages.get(lead["stage"], 0) + 1
        out.append("\nLEADS BY STAGE (%d total): " % len(leads)
                   + ", ".join(f"{k} {v}" for k, v in sorted(stages.items())))
        out.append("Money: $%d collected, $%d in payment links still out."
                   % (db.revenue_cents() // 100, db.pending_cents() // 100))

        waiting = db.leads_awaiting_approval()
        if waiting:
            out.append("\nWAITING FOR THEIR APPROVAL (%d) — on the Approve page:"
                       % len(waiting))
            for lead in waiting[:8]:
                out.append("  - %s (%s)" % (lead["name"],
                                            lead["email"] or "no email address yet"))
            if len(waiting) > 8:
                out.append("  ...and %d more." % (len(waiting) - 8))

        moving = [l for l in leads if l["stage"] in (
            core.STAGE_CONTACTED, core.STAGE_PREVIEW_SENT,
            core.STAGE_PAYMENT_LINK_SENT, core.STAGE_PAID)]
        if moving:
            out.append("\nDEALS IN FLIGHT:")
            for lead in moving[:10]:
                out.append("  - %s: %s" % (lead["name"], lead["stage"]))

        stuck = [l for l in leads if l["stage"] == core.STAGE_ERROR]
        if stuck:
            out.append("\nERRORED (retryable from the lead's page):")
            for lead in stuck[:5]:
                out.append("  - %s: %s" % (lead["name"], (lead["error"] or "")[:160]))

    attention = db.attention_events()
    if attention:
        out.append("\nNEEDS THEIR ATTENTION (%d):" % len(attention))
        for ev in attention[:6]:
            out.append("  - %s" % (ev["detail"] or ev["kind"])[:200])

    recent = db.recent_events(12)
    if recent:
        out.append("\nRECENT ACTIVITY (newest first):")
        for ev in recent:
            out.append("  - [%s] %s: %s" % (ev["created_at"][11:16], ev["kind"],
                                            (ev["detail"] or "")[:140]))
    return "\n".join(out)


KEY_FIELDS = [
    {
        "field": "anthropic_api_key",
        "name": "Anthropic (Claude)",
        "job": "Writes the replies and designs each website.",
        "hint": "sk-ant-…",
        "url": "https://console.anthropic.com/settings/keys",
        "site": "console.anthropic.com",
        "minutes": "2 min",
        "steps": [
            "Sign in, then click <b>Create Key</b>.",
            "Name it <i>Solo Studio</i> and click <b>Add</b>.",
            "Copy the key <b>now</b> — the site won't show it again.",
        ],
        "extra_url": "https://console.anthropic.com/settings/billing",
        "extra_label": "Add credit",
        "note": "Pay-as-you-go, and separate from a Claude Pro or Max "
                "subscription — paying for Claude in the browser buys you "
                "nothing here. Add $5 of credit under Billing to start; "
                "designing a site costs cents, not dollars. Make sure the "
                "credit goes to the same account the key came from: if the "
                "switcher at the top-left of the console offers more than one, "
                "they each have their own balance.",
    },
    {
        "field": "inkbox_api_key",
        "name": "Inkbox",
        "job": "The mailbox that sends your emails and reads the replies.",
        "hint": "",
        "url": "https://inkbox.ai/console",
        "site": "inkbox.ai",
        "minutes": "3 min",
        "steps": [
            "Sign up, then create an identity — this gives Solo Studio its own "
            "email address.",
            "Open the console's API keys section and create a key.",
            "Copy it here.",
        ],
        "note": "Email only. Inkbox blocks cold text messages on purpose, so "
                "texting isn't part of the pipeline.",
    },
    {
        "field": "netlify_api_key",
        "name": "Netlify",
        "job": "Puts each website online at a real web address.",
        "hint": "nfp_…",
        "url": "https://app.netlify.com/user/applications#personal-access-tokens",
        "site": "app.netlify.com",
        "minutes": "2 min",
        "steps": [
            "Sign in — the page opens on <b>Applications</b>.",
            "Under <b>Personal access tokens</b> click <b>New access token</b>.",
            "Name it <i>Solo Studio</i>, leave the expiry as-is, click "
            "<b>Generate token</b>, and copy it.",
        ],
        "note": "Netlify's free tier is plenty for the sites you'll be selling.",
    },
    {
        "field": "yelp_api_key",
        "name": "Yelp",
        "optional": True,
        "job": "A fourth index of local businesses, for coverage the others miss.",
        "hint": "",
        "url": "https://docs.developer.yelp.com/docs/fusion-intro",
        "site": "Yelp for Developers",
        "minutes": "5 min \u00b7 optional",
        "steps": [
            "Create a free developer account and add an app.",
            "Copy the <b>API Key</b> (not the client ID).",
        ],
        "note": "Free tier, a few hundred calls a day. Worth knowing: Yelp "
                "gives us their Yelp page, never their own website \u2014 so "
                "these leads are marked as a directory page only, a weaker "
                "signal than Google or OpenStreetMap where the real site got "
                "checked. Everything works without this.",
    },
    {
        "field": "hunter_api_key",
        "name": "Hunter",
        "optional": True,
        "job": "Finds email addresses behind a domain we already know.",
        "hint": "",
        "url": "https://hunter.io/api-keys",
        "site": "hunter.io",
        "minutes": "3 min \u00b7 optional",
        "steps": [
            "Sign up free (25 searches a month), then open <b>API Keys</b>.",
            "Copy the key.",
        ],
        "note": "Only helps for businesses whose website is dead or parked "
                "\u2014 those have a domain to look up. A business with no "
                "website at all has no domain, and Claude's web search handles "
                "those. Everything works without this.",
    },
    {
        "field": "stripe_secret_key",
        "name": "Stripe",
        "job": "Takes the payment. Nothing ships until Stripe says it cleared.",
        "hint": "sk_test_… to practise, sk_live_… for real money",
        "url": "https://dashboard.stripe.com/test/apikeys",
        "site": "dashboard.stripe.com",
        "minutes": "3 min",
        "steps": [
            "Sign up. The link opens <b>Test mode</b> — stay there for now.",
            "Find <b>Secret key</b>, click <b>Reveal test key</b>, copy it.",
            "Paste it here and practise the whole flow with the test card "
            "<code>4242 4242 4242 4242</code>, any future expiry, any CVC.",
        ],
        "note": "Start with the TEST key (sk_test_…). Swap to your live key only "
                "once you've watched a fake sale go through end to end.",
    },
    {
        "field": "google_places_api_key",
        "name": "Google Places",
        "job": "Finds local businesses that don't have a website yet.",
        "hint": "AIza…",
        "url": "https://console.cloud.google.com/",
        "site": "console.cloud.google.com",
        "minutes": "10 min",
        "steps": [
            "Click the project dropdown at the top → <b>New Project</b> → name it "
            "<i>Solo Studio</i> → <b>Create</b>.",
            "Menu → <b>Billing</b> → link a card. Google requires one for Places; "
            "normal use stays inside the free monthly allowance.",
            "Menu → <b>APIs &amp; Services → Library</b> → search "
            "<b>Places API (New)</b> → <b>Enable</b>.",
            "Menu → <b>APIs &amp; Services → Credentials</b> → "
            "<b>Create credentials → API key</b> → copy it.",
        ],
        "note": "The fiddliest one, and the only one that needs a card on file. "
                "Save it for last — everything else works without it, you'd just "
                "be adding businesses by hand.",
    },
]


@app.get("/ask")
def ask_page():
    return _render(ASK, history=STATE.db.chat_history(),
                   has_key=bool(STATE.config.get("anthropic_api_key")))


@app.post("/ask/send")
def ask_send():
    """Answer one question. Advisory only — nothing here can act on the pipeline."""
    message = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Ask me something first.")
    if len(message) > 4000:
        message = message[:4000]
    if not STATE.config.get("anthropic_api_key"):
        return jsonify(ok=False, error="Add your Anthropic (Claude) key on the "
                                       "Setup page and I can start answering.")
    db = STATE.db
    db.chat_add("user", message)
    history = [{"role": m["role"], "content": m["content"]}
               for m in db.chat_history()]
    try:
        reply = STATE.services.assistant_reply(history, assistant_snapshot())
    except Exception as e:                       # network, bad key, rate limit
        return jsonify(ok=False, error=f"Couldn't reach Claude: {core.explain(e)}")
    db.chat_add("assistant", reply)
    return jsonify(ok=True, reply=reply)


@app.get("/ask/history")
def ask_history():
    """The saved conversation, so JARVIS opens on the same thread as /ask."""
    return jsonify(ok=True, has_key=bool(STATE.config.get("anthropic_api_key")),
                   messages=[{"role": m["role"], "content": m["content"]}
                             for m in STATE.db.chat_history()])


@app.post("/ask/clear")
def ask_clear():
    STATE.db.chat_clear()
    return redirect(url_for("ask_page"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        cfg = core.load_config()
        for spec in KEY_FIELDS:
            field = spec["field"]
            val = (request.form.get(field) or "").strip()
            if val:  # blank = keep existing
                cfg[field] = val
        for field in ("inkbox_agent_handle", "anthropic_model", "your_name",
                      "studio_name", "mailing_address", "outreach_subject",
                      "outreach_body"):
            if field in request.form:
                cfg[field] = request.form.get(field, "")
        for field, cast, lo in (("site_price_usd", float, 1),
                                ("poll_interval_seconds", int, 30)):
            try:
                cfg[field] = max(lo, cast(request.form.get(field, "")))
            except (TypeError, ValueError):
                pass
        cfg["auto_search_enabled"] = bool(request.form.get("auto_search_enabled"))
        cfg["auto_research_enabled"] = bool(request.form.get("auto_research_enabled"))
        quality = request.form.get("lead_quality", "")
        if quality in core.QUALITY_LEVELS:
            cfg["lead_quality"] = quality
        if "saved_searches" in request.form:
            cfg["saved_searches"] = request.form.get("saved_searches", "")
        for field, lo, hi in (("monthly_lookup_cap", 0, 5000),
                              ("lead_target", 10, 20000),
                              ("tiles_per_tick", 1, 20),
                              ("search_interval_hours", 1, 168),
                              ("searches_per_run", 1, 60),
                              ("daily_send_cap", 1, 200)):
            try:
                cfg[field] = max(lo, min(hi, int(request.form.get(field, ""))))
            except (TypeError, ValueError):
                pass
        cfg["phone_access_enabled"] = bool(request.form.get("phone_access_enabled"))
        pin_val = (request.form.get("phone_pin") or "").strip()
        cfg["phone_pin"] = pin_val if pin_val.isdigit() and 4 <= len(pin_val) <= 8 \
            else ("" if not pin_val else cfg.get("phone_pin", ""))
        cfg["ntfy_enabled"] = bool(request.form.get("ntfy_enabled"))
        if cfg["ntfy_enabled"] and not cfg.get("ntfy_topic"):
            cfg["ntfy_topic"] = "solo-studio-" + secrets.token_hex(8)
        core.save_config(cfg)
        STATE.reload()
        flash("Settings saved.", "ok")
        return redirect(url_for("setup"))
    ip = lan_ip()
    target = request.url_root if CLOUD_MODE else f"http://{ip}:{PORT}/"
    needed = sum(1 for k in KEY_FIELDS if not k.get("optional"))
    have = sum(1 for k in KEY_FIELDS if STATE.config.get(k["field"])
               and not k.get("optional"))
    trades_text = "\n".join(core.DEFAULT_TRADES)
    price = core.fmt_price(STATE.config.get("site_price_usd", 500))
    return _render(SETUP, key_fields=KEY_FIELDS, lan_ip=ip,
                   phone_listening=(CLOUD_MODE or BOUND_HOST == "0.0.0.0"),
                   spend=_spend_so_far(),
                   keys_have=have, keys_needed=needed, keys_missing=needed - have,
                   trades_text=trades_text, cost=_search_cost(STATE.config),
                   price_value=price,
                   phone_qr=qr_svg(target))


def _search_cost(cfg) -> dict:
    """What the saved searches will actually cost per month.

    Google bills Text Search per call at roughly $32/1,000 with the first
    5,000 a month free, and each search pages up to three times. Showing this
    is the difference between a helpful feature and a surprise bill.
    """
    queries = len([q for q in (cfg.get("saved_searches") or "").splitlines()
                   if q.strip()])
    per_run = max(1, int(cfg.get("searches_per_run", 20) or 20))
    hours = max(1, int(cfg.get("search_interval_hours", 12) or 12))
    per_run = min(per_run, queries) if queries else 0
    runs_month = (24 / hours) * 30.4
    calls = int(per_run * PAGES_PER_SEARCH * runs_month)
    over = max(0, calls - FREE_CALLS_MONTH)
    return {
        "searches": queries, "per_run": per_run, "hours": hours,
        "calls_month": calls, "over": over > 0,
        "dollars": f"{over / 1000 * DOLLARS_PER_1K:.0f}",
        "days_for_full_sweep": max(
            1, round(queries / per_run * hours / 24)) if per_run else 1,
    }


@app.post("/action/build_searches")
def build_searches():
    """Turn 'my town + how far I'd drive' into the actual list of searches."""
    base = (request.form.get("territory_base") or "").strip()
    trades = [t.strip() for t in (request.form.get("trades") or "").splitlines()
              if t.strip()]
    try:
        miles = max(5, min(120, int(request.form.get("territory_miles") or 30)))
    except (TypeError, ValueError):
        miles = 30
    if not base:
        flash("Put your town in first — something like \"Napanoch, NY\".", "err")
        return redirect(url_for("setup"))
    if not trades:
        flash("Give it at least one trade to look for.", "err")
        return redirect(url_for("setup"))
    if not STATE.config.get("anthropic_api_key"):
        flash("This uses your Anthropic key to look up the towns — add it "
              "further up the page first.", "err")
        return redirect(url_for("setup"))
    try:
        towns = STATE.services.towns_near(base, miles)
    except Exception as e:
        flash(f"Couldn't work out the towns: {core.explain(e)}", "err")
        return redirect(url_for("setup"))

    # Order matters more than it looks. A run only spends a fixed budget of
    # searches, so listing every trade in one town before moving on meant the
    # first run never left that town — and the town people type is the big
    # city, where every business already has a website. Rotate instead: each
    # sweep visits all the towns, with a different trade each time, so one run
    # covers as much ground as it has searches.
    lines = [f"{trades[(j + k) % len(trades)]} in {towns[j]}"
             for k in range(len(trades)) for j in range(len(towns))]
    cfg = core.load_config()
    cfg.update(territory_base=base, territory_miles=miles,
               saved_searches="\n".join(lines))
    core.save_config(cfg)
    STATE.reload()
    STATE.db.set_kv("search_cursor", "0")
    flash(f"Built {len(lines)} searches — {len(trades)} trades across "
          f"{len(towns)} towns within {miles} miles of {base}. Check the cost "
          "note before you switch automatic searching on.", "ok")
    return redirect(url_for("setup"))


@app.get("/setup/test_notification")
def test_notification():
    try:
        STATE.services.push_notify(
            "Solo Studio test", "Notifications are working! You'll get a buzz "
            "for replies, previews, and payments.", tags="white_check_mark")
        flash("Test notification sent — check your phone (subscribe to the topic "
              "in the ntfy app first).", "ok")
    except Exception as e:
        flash(f"Couldn't send: {core.explain(e, 300)}", "err")
    return redirect(url_for("setup"))


@app.get("/setup/test")
def setup_test():
    results = []
    cfg = STATE.config
    svc = core.Services(cfg)

    def run(name, fn, missing_key):
        if missing_key:
            results.append((name, False, "No key saved yet."))
            return
        try:
            detail = fn()
            results.append((name, True, detail))
        except Exception as e:
            results.append((name, False, core.explain(e, 300)))

    def test_places():
        r = svc.places_search_no_website("coffee in San Francisco", max_results=1)
        return f"Search worked ({len(r)} no-website result in sample)."

    def test_inkbox():
        ident = svc._get_identity()
        return f"Connected as {ident.agent_handle} <{ident.email_address}>."

    def test_anthropic():
        client = svc._get_anthropic()
        client.messages.count_tokens(
            model=cfg.get("anthropic_model") or "claude-opus-5",
            messages=[{"role": "user", "content": "ping"}])
        return f"Key valid; model {cfg.get('anthropic_model')} reachable."

    def test_netlify():
        import requests as rq
        r = rq.get("https://api.netlify.com/api/v1/user",
                   headers=svc._netlify_headers(), timeout=15)
        if r.status_code != 200:
            raise core.ServiceError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Connected as {r.json().get('email', 'unknown')}."

    def test_stripe():
        import requests as rq
        r = rq.get("https://api.stripe.com/v1/balance",
                   headers=svc._stripe_auth(), timeout=15)
        if r.status_code != 200:
            raise core.ServiceError(f"HTTP {r.status_code}: {r.text[:200]}")
        mode = "TEST mode" if cfg.get("stripe_secret_key", "").startswith("sk_test") \
            else "LIVE mode"
        return f"Key valid ({mode})."

    run("Google Places", test_places, not cfg.get("google_places_api_key"))
    run("Inkbox email", test_inkbox, not cfg.get("inkbox_api_key"))
    run("Claude", test_anthropic, not cfg.get("anthropic_api_key"))
    run("Netlify", test_netlify, not cfg.get("netlify_api_key"))
    run("Stripe", test_stripe, not cfg.get("stripe_secret_key"))
    return _render(SETUP_TEST, results=results)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or PORT))
    args = parser.parse_args()

    if CLOUD_MODE:  # server deployment: bind publicly, no browser, no port probe
        start_autopilot_thread()
        print(f"Solo Studio (cloud mode) on port {args.port}")
        app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
        return

    url = f"http://127.0.0.1:{args.port}/"
    global RUN_PORT
    RUN_PORT = args.port
    if args.port == PORT and _port_in_use():
        # Another copy is already running — just show it.
        if args.open_browser:
            webbrowser.open(url)
            return
        print(f"Solo Studio is already running at {url}")
        return

    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    start_autopilot_thread()
    cfg = STATE.config
    global BOUND_HOST
    host = "0.0.0.0" if (cfg.get("phone_access_enabled")
                         and cfg.get("phone_pin")) else "127.0.0.1"
    BOUND_HOST = host
    if host == "0.0.0.0":
        print(f"Phone access ON — from your phone: http://{lan_ip()}:{args.port}/")
    print(f"Solo Studio dashboard: {url}")
    app.run(host=host, port=args.port, debug=False, use_reloader=False)


if CLOUD_MODE:
    # Under a WSGI server main() never runs, so start the worker at import.
    start_autopilot_thread()


if __name__ == "__main__":
    main()
