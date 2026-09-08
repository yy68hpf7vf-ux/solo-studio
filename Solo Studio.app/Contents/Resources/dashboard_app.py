"""Solo Studio dashboard — Flask web UI wrapping solo_studio_agent.

Run:  python3 dashboard_app.py [--open-browser]

Everything is configured on the Setup page (saved to config.json) — no code
editing, no environment variables. The dashboard binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import base64
import hmac
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
    last_run = 0.0
    while True:
        time.sleep(5)
        try:
            cfg = STATE.config
            if not cfg.get("autopilot_enabled"):
                continue
            if not cfg.get("inkbox_api_key"):
                continue
            interval = max(30, int(cfg.get("poll_interval_seconds", 120)))
            if time.time() - last_run < interval:
                continue
            last_run = time.time()
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
  background:linear-gradient(180deg,var(--bg2),var(--bg) 52%)}

/* The living backdrop. Drawn small and crisp, then blurred by the compositor —
   far cheaper than painting soft edges in canvas, and it is what gives the
   ribbons their glow. */
/* The living backdrop.

   Every frame of this is composited, never repainted. Each ribbon is drawn into
   a small canvas once, at load, and from then on only moves — CSS transforms the
   browser can hand straight to the compositor. Redrawing a full-screen canvas
   every frame instead measured 8fps against 60 on a machine without a GPU, and
   plenty of phones are that machine when they are hot or saving battery.

   Softness comes from the drawing (layered strokes) and from the browser
   smoothing a small texture up to full size — not from a CSS blur, which is the
   single most expensive thing a page like this can ask for. */
#aurora{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden;
  opacity:.5;transition:opacity 1.2s ease;
  transform:translate3d(calc(var(--px,0) * 26px), calc(var(--py,0) * 20px), 0)}
#aurora canvas{position:absolute;left:-30%;width:160%;height:64vh;
  will-change:transform;mix-blend-mode:screen}
/* four depths: the further back, the slower and the less it answers the pointer */
#aurora .l0{top:-14vh;animation:drift0 46s ease-in-out infinite;
  transform:translate3d(calc(var(--px,0) * 6px), calc(var(--py,0) * 5px),0)}
#aurora .l1{top:12vh; animation:drift1 61s ease-in-out infinite}
#aurora .l2{top:44vh; animation:drift2 53s ease-in-out infinite}
#aurora .l3{top:70vh; animation:drift3 39s ease-in-out infinite}
@keyframes drift0{0%,100%{transform:translate3d(-5%,0,0)   rotate(-1.6deg) scale(1.04)}
                  50%    {transform:translate3d( 5%,2.5vh,0) rotate( 1.4deg) scale(1.12)}}
@keyframes drift1{0%,100%{transform:translate3d( 6%,1.5vh,0) rotate( 1.8deg) scale(1.08)}
                  50%    {transform:translate3d(-6%,-2vh,0)  rotate(-1.2deg) scale(1.0)}}
@keyframes drift2{0%,100%{transform:translate3d(-4%,-1vh,0)  rotate( 1.1deg) scale(1.0)}
                  50%    {transform:translate3d( 7%,2vh,0)   rotate(-1.7deg) scale(1.1)}}
@keyframes drift3{0%,100%{transform:translate3d( 3%,2vh,0)   rotate(-1.3deg) scale(1.12)}
                  50%    {transform:translate3d(-7%,-1.5vh,0) rotate( 1.6deg) scale(1.02)}}
/* the whole field brightens and swells for a moment when something happens */
#aurora.pulse{animation:auroraPulse 2.4s ease-out}
@keyframes auroraPulse{0%{opacity:.5}18%{opacity:.78}100%{opacity:.5}}

/* Set by the watchdog below when the device can't keep up. Tier 1 halves the
   moving layers; tier 2 stops the drift and leaves the ribbons standing still,
   which still looks like the desktop, just without the breathing. */
#aurora.tier1 .l1,#aurora.tier1 .l3{display:none}
#aurora.tier2 canvas{animation:none!important}
#aurora.tier2{transform:none!important}

/* Depth: panels sit above the light and lean very slightly toward the pointer.
   Kept under two degrees — enough to feel physical, not enough to smear text. */
.card,.stat{transform-style:preserve-3d;
  transition:transform .35s cubic-bezier(.22,.61,.36,1),
             box-shadow .35s, border-color .35s}
.card.lift{box-shadow:0 1px 0 rgba(210,195,255,.08) inset,
  0 22px 62px rgba(0,0,0,.55), 0 0 46px rgba(244,114,182,.14);
  border-color:rgba(244,114,182,.22)}
.stat.lift{border-color:rgba(244,114,182,.3);
  box-shadow:0 10px 30px rgba(0,0,0,.45)}
/* the specular sheen that tracks the pointer across a panel */
.card{position:relative;overflow:hidden}
.card::after{content:"";position:absolute;inset:0;pointer-events:none;
  opacity:0;transition:opacity .35s;border-radius:inherit;
  background:radial-gradient(420px circle at var(--mx,50%) var(--my,0%),
    rgba(244,114,182,.10), transparent 62%)}
.card.lift::after{opacity:1}

@media (prefers-reduced-motion:reduce){
  #aurora,#aurora canvas{animation:none!important;transform:none!important}
  #aurora{opacity:.55}
  body::before{background:
    radial-gradient(58vw 42vw at 78% -6%, rgba(244,114,182,.11), transparent 62%),
    linear-gradient(180deg,var(--bg2),var(--bg) 52%)}
  .card,.stat{transition:none}
}

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
<div id="aurora" aria-hidden="true">
  <canvas class="l0"></canvas><canvas class="l1"></canvas>
  <canvas class="l2"></canvas><canvas class="l3"></canvas>
</div>
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
{% block body %}{% endblock %}
</main>
<div id="live-pill" hidden>New activity — tap to refresh</div>
<span id="live-stamp" hidden data-stamp="{{ live_stamp }}"></span>
<script>
/* ---------------------------------------------------------------------------
   The living backdrop.

   Ribbons of light drifting behind the app, in the spirit of the Mac desktop.
   Three things move them: time, where your pointer is (each ribbon at its own
   depth, so the field parallaxes), and the business itself — how much is in
   flight sets the energy, and a payment or a new reply sends a bright pulse
   rolling through.

   Deliberately cheap: half-resolution buffer, crisp strokes blurred by CSS,
   30fps, asleep whenever the tab is hidden, and switched off entirely for
   anyone who asked for reduced motion.
--------------------------------------------------------------------------- */
(function () {
  var field = document.getElementById('aurora');
  if (!field) return;
  var layers = [].slice.call(field.querySelectorAll('canvas'));
  if (!layers.length) return;
  var slow = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)');

  /* Four bands of ribbons. Each is painted once; after that the CSS moves it. */
  var BANDS = [
    { ribbons: 2, hue: [244,114,182], width: 30, alpha: 0.17 },
    { ribbons: 2, hue: [129,140,248], width: 22, alpha: 0.15 },
    { ribbons: 2, hue: [192,132,252], width: 34, alpha: 0.16 },
    { ribbons: 2, hue: [ 99, 91,200], width: 42, alpha: 0.12 }
  ];
  var W = 360, H = 200;                       /* tiny — CSS smooths it up to full size */

  function paint(canvas, band, seed) {
    canvas.width = W; canvas.height = H;
    var g = canvas.getContext('2d');
    if (!g) return;
    g.clearRect(0, 0, W, H);
    g.globalCompositeOperation = 'lighter';
    g.lineCap = 'round'; g.lineJoin = 'round';

    for (var i = 0; i < band.ribbons; i++) {
      var y = H * (0.26 + i * 0.26) + Math.sin(seed + i * 2.3) * 16,
          sway = 28 + Math.sin(seed * 1.7 + i) * 14;
      g.beginPath();
      g.moveTo(-30, y);
      g.bezierCurveTo(W * 0.22, y - sway, W * 0.42, y + sway * 1.2, W * 0.6, y - sway * 0.3);
      g.bezierCurveTo(W * 0.76, y - sway * 1.3, W * 0.9, y + sway * 0.7, W + 30, y);
      /* wide+faint, then tight+bright: a soft falloff with no filter involved */
      for (var pass = 0; pass < 3; pass++) {
        g.lineWidth = band.width * [2.2, 1.3, 0.62][pass];
        g.strokeStyle = 'rgba(' + band.hue[0] + ',' + band.hue[1] + ',' + band.hue[2]
                      + ',' + (band.alpha * [0.26, 0.5, 1][pass]).toFixed(3) + ')';
        g.stroke();
      }
    }
  }
  layers.forEach(function (c, i) { paint(c, BANDS[i], i * 1.9); });

  /* Pointer parallax. One CSS variable, written at most once a frame, so the
     browser moves existing layers instead of redrawing anything. */
  if (!(slow && slow.matches)) {
    var tx = 0, ty = 0, cx = 0, cy = 0, queued = false;
    function apply() {
      queued = false;
      cx += (tx - cx) * 0.08;
      cy += (ty - cy) * 0.08;
      field.style.setProperty('--px', cx.toFixed(3));
      field.style.setProperty('--py', cy.toFixed(3));
      if (Math.abs(tx - cx) > 0.002 || Math.abs(ty - cy) > 0.002) nudge();
    }
    function nudge() { if (!queued) { queued = true; requestAnimationFrame(apply); } }
    addEventListener('pointermove', function (e) {
      tx = (e.clientX / innerWidth - 0.5) * 2;
      ty = (e.clientY / innerHeight - 0.5) * 2;
      nudge();
    }, { passive: true });
    addEventListener('deviceorientation', function (e) {
      if (e.gamma == null) return;
      tx = Math.max(-1, Math.min(1, e.gamma / 40));
      ty = Math.max(-1, Math.min(1, (e.beta - 45) / 40));
      nudge();
    }, { passive: true });
  }

  /* Whether any of this is affordable is a property of the device, not of the
     code — a hot phone in battery saver is a different machine from the same
     phone plugged in. So measure real frames here and step down if the page
     can't hold a smooth rate. Cheap to run, and it only ever runs twice. */
  function watchdog(rechecks) {
    var frames = 0, t0 = performance.now();
    (function tick() {
      frames++;
      var dt = performance.now() - t0;
      if (dt < 1200) { requestAnimationFrame(tick); return; }
      var fps = frames / (dt / 1000),
          cls = field.classList;
      if (fps < 45 && !cls.contains('tier2')) {
        /* step down one level and look again — repeat until it is smooth */
        cls.add(cls.contains('tier1') ? 'tier2' : 'tier1');
        setTimeout(function () { watchdog(rechecks); }, 700);
        return;
      }
      /* smooth: check again later, in case the device gets hot or throttles */
      if (rechecks > 0) setTimeout(function () { watchdog(rechecks - 1); }, 25000);
    })();
  }
  if (!(slow && slow.matches)) setTimeout(function () { watchdog(2); }, 900);

  /* What the business is doing, from the poll below. Opacity and drift speed
     only — both compositor properties, so this costs nothing per frame. */
  window.__solo = {
    state: function (d) {
      if (!d) return;
      var busy = (d.active || 0) * 0.12 + (d.pending || 0) * 0.05
               + (d.attention || 0) * 0.08 + Math.min(0.3, (d.revenue || 0) / 6000);
      var energy = Math.max(0, Math.min(1, busy));
      field.style.opacity = (0.4 + energy * 0.3).toFixed(2);
      layers.forEach(function (c, i) {
        var base = [46, 61, 53, 39][i];
        c.style.animationDuration = (base * (1 - energy * 0.4)).toFixed(1) + 's';
      });
    },
    pulse: function () {
      field.classList.remove('pulse');
      void field.offsetWidth;                 /* restart the keyframe */
      field.classList.add('pulse');
    }
  };
})();
</script>

<script>
/* Panels lean toward the pointer and catch a highlight — the depth you can
   feel rather than see. Pointer devices only, and never under reduced motion. */
(function () {
  var slow = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  if (slow && slow.matches) return;
  if (!window.matchMedia || !matchMedia('(hover: hover) and (pointer: fine)').matches) return;

  function bind(el, maxTilt) {
    el.addEventListener('pointermove', function (e) {
      var r = el.getBoundingClientRect();
      var cx = (e.clientX - r.left) / r.width, cy = (e.clientY - r.top) / r.height;
      el.style.setProperty('--mx', (cx * 100).toFixed(1) + '%');
      el.style.setProperty('--my', (cy * 100).toFixed(1) + '%');
      el.style.transform =
        'perspective(1100px) rotateX(' + ((0.5 - cy) * maxTilt).toFixed(2) + 'deg)' +
        ' rotateY(' + ((cx - 0.5) * maxTilt).toFixed(2) + 'deg)' +
        ' translateZ(0)';
      el.classList.add('lift');
    }, { passive: true });
    el.addEventListener('pointerleave', function () {
      el.style.transform = '';
      el.classList.remove('lift');
    });
  }
  document.querySelectorAll('.card').forEach(function (el) { bind(el, 1.6); });
  document.querySelectorAll('.stat').forEach(function (el) { bind(el, 5); });
})();
</script>

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
        if (window.__solo) window.__solo.state(d);
        var stamp = d.last_event + ':' + d.pending + ':' + d.attention;
        if (!seen) { seen = stamp; return; }   /* no baseline: adopt this one */
        if (stamp === seen) return;
        if (window.__solo) window.__solo.pulse();   /* something happened */
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
{% elif not config.autopilot_enabled %}
<div class="warnbar"><div><b>Autopilot is OFF.</b> Replies and payments are not
being processed automatically. Turn it on, or use “Check now”.</div>
<form class="inline" method="post" action="{{ url_for('toggle_autopilot') }}">
<button class="btn btn-primary">Turn autopilot on</button></form></div>
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
    placeholder='e.g. "plumbers in Riverside, CA" — businesses with no website are kept'>
  <button class="btn btn-primary" style="white-space:nowrap">Search Google Places</button>
</form>
<p class="muted" style="margin-bottom:0">Google Places doesn’t publish email
addresses, so new leads need an email added (look them up — Yelp, Facebook,
phone call) before outreach can go out. Found businesses queue up on the
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
  {% if keys_missing %}{{ keys_have }} of {{ key_fields|length }} keys saved —
    {{ keys_missing }} to go
    <div class="sub">Work down the list. Each one opens the right page for you.</div>
  {% else %}All {{ key_fields|length }} keys saved
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
<label><input type="checkbox" name="auto_research_enabled" value="1"
  {% if config.auto_research_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Let the Researcher hunt missing emails</label>
<p class="muted">Uses Claude's web search to find each business's public contact
address. It only ever suggests — you accept or reject each one.</p>
<label>Searches per run</label>
<input type="number" name="searches_per_run" min="1" max="60"
  value="{{ config.searches_per_run or 10 }}">
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
            "call_count": callable_now,
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
.right{grid-area:right;display:flex;flex-direction:column;justify-content:center;gap:12px}
.bar .label{display:flex;justify-content:space-between;margin-bottom:3px}
.bar .label span:last-child{color:var(--cy2)}
.track{height:7px;background:rgba(165,180,252,.10);border-radius:2px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,rgba(165,180,252,.35),var(--cy));
  box-shadow:0 0 10px rgba(165,180,252,.6);width:0;transition:width .9s ease}

/* ---- mission log ---- */
.feed{grid-area:feed;border-top:1px solid rgba(165,180,252,.22);padding-top:10px;
  overflow:hidden}
.feed .label{margin-bottom:8px}
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
  <div class="right" id="bars"></div>
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
    typeOut(document.getElementById('greet'),
            greetWord + (d.owner ? ', ' + d.owner : '') + '. All services standing by.');

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

    document.getElementById('coreState').textContent =
      d.attention > 0 ? d.attention + (d.attention === 1 ? ' ITEM NEEDS' : ' ITEMS NEED') + ' YOU'
                      : 'SYSTEMS NOMINAL';

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
    _live_stamp); the rest feed the living backdrop, so the room reacts to the
    business rather than to nothing."""
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
                   attention=db.attention_events(), configured=configured)



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
  <div class="muted" style="margin:6px 0"><b>To:</b> {{ item.lead['email'] }}</div>
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
    queue = [{"lead": lead, "rendered": STATE.agent.render_outreach(lead)}
             for lead in db.leads_awaiting_approval()]
    cap = int(STATE.config.get("daily_send_cap", 20) or 0)
    sent_today = db.sends_today()
    remaining = len(queue)
    if cap:
        remaining = max(0, min(remaining, cap - sent_today))
    return _render(APPROVE_PAGE, queue=queue, needs_email=db.leads_needing_email(),
                   cap=cap, sent_today=sent_today, remaining=remaining)


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
    try:
        r = STATE.agent.run_saved_searches(force=True)
        if r.get("skipped"):
            flash(f"Nothing to do — {r['skipped']}. Add searches in Setup.", "err")
        else:
            flash(f"Search finished: {r.get('added', 0)} new leads added.", "ok")
    except Exception as e:
        flash(str(e), "err")
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
    try:
        r = STATE.agent.research_missing_emails(force=True, limit=5)
        flash(f"Researcher checked {r.get('researched', 0)} businesses and found "
              f"{r.get('found', 0)} email addresses.", "ok")
    except Exception as e:
        flash(str(e), "err")
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


@app.post("/action/find_leads")
def find_leads():
    query = (request.form.get("query") or "").strip()
    if not query:
        flash("Type a search first.", "err")
        return redirect(url_for("dashboard"))
    try:
        r = STATE.agent.find_leads(query)
        flash(f"Found {r['found']} businesses without websites; {r['added']} new "
              "leads added.", "ok")
    except Exception as e:
        flash(str(e), "err")
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

    missing = [k["name"] for k in KEY_FIELDS if not cfg.get(k["field"])]
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
        if "saved_searches" in request.form:
            cfg["saved_searches"] = request.form.get("saved_searches", "")
        for field, lo, hi in (("search_interval_hours", 1, 168),
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
    have = sum(1 for k in KEY_FIELDS if STATE.config.get(k["field"]))
    trades_text = "\n".join(core.DEFAULT_TRADES)
    price = core.fmt_price(STATE.config.get("site_price_usd", 500))
    return _render(SETUP, key_fields=KEY_FIELDS, lan_ip=ip,
                   phone_listening=(CLOUD_MODE or BOUND_HOST == "0.0.0.0"),
                   keys_have=have, keys_missing=len(KEY_FIELDS) - have,
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
    per_run = max(1, int(cfg.get("searches_per_run", 10) or 10))
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

    lines = [f"{trade} in {town}" for town in towns for trade in trades]
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
