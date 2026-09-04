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
import secrets
import socket
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import timedelta

from flask import (Flask, abort, flash, redirect, render_template_string,
                   request, session, url_for)

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
CLOUD_PASSWORD = os.environ.get("SOLO_STUDIO_PASSWORD", "").strip()
CLOUD_MODE = bool(CLOUD_PASSWORD)
MIN_CLOUD_PASSWORD = 10


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
    '<meta name="theme-color" content="#04101d">'
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
                STATE.db.log(None, "autopilot_error", str(e)[:500])
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
:root { --bg:#f5f7fa; --card:#fff; --ink:#17222e; --mut:#68788a; --line:#e3e9ef;
        --acc:#2563eb; --ok:#16a34a; --warn:#d97706; --bad:#dc2626; }
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header{background:#101826;color:#fff;padding:14px 24px;display:flex;gap:24px;
  align-items:center}
header .brand{font-weight:700;font-size:17px} header a{color:#cbd5e1;
  text-decoration:none;font-weight:500} header a:hover{color:#fff}
main{max-width:1100px;margin:24px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:18px}
h1{font-size:20px;margin:0 0 12px} h2{font-size:16px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;
  letter-spacing:.04em}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
  font-weight:600;white-space:nowrap}
.b-found{background:#eef2ff;color:#4338ca} .b-contacted{background:#e0f2fe;color:#0369a1}
.b-preview_sent{background:#fef9c3;color:#854d0e}
.b-payment_link_sent{background:#ffedd5;color:#c2410c}
.b-paid,.b-delivered{background:#dcfce7;color:#15803d}
.b-not_interested{background:#f1f5f9;color:#64748b}
.b-error{background:#fee2e2;color:#b91c1c}
.b-building_preview,.b-sending_payment_link,.b-deploying_final{background:#ede9fe;color:#6d28d9}
.btn{display:inline-block;border:1px solid var(--line);background:#fff;color:var(--ink);
  border-radius:7px;padding:6px 12px;font-size:13px;font-weight:600;cursor:pointer;
  text-decoration:none}
.btn:hover{border-color:var(--acc);color:var(--acc)}
.btn-primary{background:var(--acc);border-color:var(--acc);color:#fff}
.btn-primary:hover{opacity:.9;color:#fff}
.btn-danger{color:var(--bad)} .btn-sm{padding:3px 9px;font-size:12px}
form.inline{display:inline} input[type=text],input[type=password],input[type=number],
textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);
  border-radius:7px;font:inherit;background:#fff}
textarea{min-height:120px}
label{display:block;font-weight:600;font-size:13px;margin:12px 0 4px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:500}
.flash.ok{background:#dcfce7;color:#166534} .flash.err{background:#fee2e2;color:#991b1b}
.muted{color:var(--mut);font-size:13px}
.warnbar{background:#fff7ed;border:1px solid #fdba74;color:#9a3412;border-radius:10px;
  padding:12px 16px;margin-bottom:18px;display:flex;justify-content:space-between;
  align-items:center;gap:12px}
.attention{border-left:4px solid var(--warn)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}
.statrow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 16px;text-align:center;min-width:96px}
.stat b{display:block;font-size:20px}
.stat span{font-size:12px;color:var(--mut)}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}
.tablewrap{overflow-x:auto}
#live-pill{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;
  background:#101826;color:#fff;padding:10px 18px;border-radius:999px;
  font-size:14px;font-weight:600;cursor:pointer;z-index:50;
  box-shadow:0 4px 16px rgba(0,0,0,.28)}
@media (max-width:800px){
  .grid{grid-template-columns:1fr}
  header{padding:10px 14px;gap:14px;flex-wrap:wrap;font-size:14px}
  header .brand{font-size:16px;width:auto}
  main{margin:14px auto;padding:0 12px}
  .card{padding:14px 15px}
  /* Stack the "needs an email" rows instead of squeezing them into columns */
  table.stack thead{display:none}
  table.stack tr{display:block;padding:10px 0;border-bottom:1px solid var(--line)}
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
    <span style="background:#f59e0b;color:#111;border-radius:999px;padding:1px 7px;
    font-size:12px;margin-left:3px">{{ pending_count }}</span>{% endif %}</a>
  <a href="{{ url_for('activity') }}">Activity</a>
  <a href="{{ url_for('setup') }}">Setup</a>
  <a href="{{ url_for('jarvis') }}" style="margin-left:auto;color:#7dd3fc">◉ JARVIS</a>
  {% if cloud_mode %}<form class="inline" method="post" action="{{ url_for('logout') }}">
  <button class="btn btn-sm" style="background:transparent;color:#cbd5e1;border-color:#334155">
  Sign out</button></form>{% endif %}
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
<tr {% if l['error'] %}style="background:#fff7f7"{% endif %}>
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
<div class="card">
<h1>Setup</h1>
<p class="muted">Everything is saved to <code>config.json</code> on this Mac.
Key fields show <code>saved ✓</code> when already stored — leave them blank to
keep the saved value.</p>
<form method="post">
<div class="grid">
<div>
<h2 style="margin-top:18px">API keys</h2>
{% for field, label, hint in key_fields %}
<label>{{ label }}
  {% if config[field] %}<span class="muted" style="font-weight:400">— saved ✓</span>{% endif %}</label>
<input type="password" name="{{ field }}" placeholder="{{ hint }}" autocomplete="off">
{% endfor %}
<label>Inkbox agent handle <span class="muted" style="font-weight:400">
  (blank = auto-detect if you have exactly one)</span></label>
<input type="text" name="inkbox_agent_handle"
  value="{{ config.inkbox_agent_handle }}">
<label>Claude model</label>
<input type="text" name="anthropic_model" value="{{ config.anthropic_model }}">
</div>
<div>
<h2 style="margin-top:18px">Your business</h2>
<label>Your name</label>
<input type="text" name="your_name" value="{{ config.your_name }}">
<label>Studio name</label>
<input type="text" name="studio_name" value="{{ config.studio_name }}">
<label>Mailing address <span class="muted" style="font-weight:400">
  (shown in cold emails — legally required for commercial email in the US)</span></label>
<input type="text" name="mailing_address" value="{{ config.mailing_address }}">
<label>Website price (USD)</label>
<input type="number" name="site_price_usd" value="{{ config.site_price_usd }}" min="1" step="1">
<label>Background check interval (seconds)</label>
<input type="number" name="poll_interval_seconds"
  value="{{ config.poll_interval_seconds }}" min="30" step="10">
</div>
</div>
<h2 style="margin-top:18px">Automatic lead hunting</h2>
<div class="grid">
<div>
<label><input type="checkbox" name="auto_search_enabled" value="1"
  {% if config.auto_search_enabled %}checked{% endif %}
  style="width:auto;margin-right:8px">Search for new leads automatically</label>
<label>Searches to run (one per line)</label>
<textarea name="saved_searches" style="min-height:90px"
  placeholder="plumbers in Riverside, CA&#10;barber shops in Riverside, CA&#10;landscapers in Corona, CA">{{ config.saved_searches }}</textarea>
</div>
<div>
<label>How often to search (hours)</label>
<input type="number" name="search_interval_hours" min="1" max="168"
  value="{{ config.search_interval_hours }}">
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
{% if config.phone_access_enabled and config.phone_pin %}
<div style="display:flex;gap:14px;align-items:flex-start;margin-top:10px">
  <div>{{ phone_qr|safe }}</div>
  <div class="muted">
    <b>Point your phone camera at this code.</b><br>
    Tap the link it offers, enter your PIN, then tap
    <b>Share&nbsp;→ Add to Home Screen</b> for an app icon.<br><br>
    Or type it in your phone's browser:<br>
    <code style="font-size:15px">http://{{ lan_ip }}:8747</code><br><br>
    Phone must be on the same Wi-Fi, and Solo Studio must be open on this Mac.
    Quit and reopen Solo Studio after turning this on.
  </div>
</div>
{% else %}<p class="muted">Tick the box, set a PIN, click Save — then quit and reopen
Solo Studio. A QR code will appear here to set up your phone.</p>{% endif %}
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
<div style="margin-top:16px;display:flex;gap:10px">
<button class="btn btn-primary">Save settings</button>
<a class="btn" href="{{ url_for('setup_test') }}">Test connections</a>
</div>
</form>
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
    return {"config": STATE.config, "pwa_meta": PWA_META,
            "cloud_mode": CLOUD_MODE, "pending_count": pending,
            "live_stamp": stamp}


def _render(tpl, **ctx):
    return render_template_string(tpl, **ctx)


JARVIS = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS — Solo Studio</title>
PWAMETA_PLACEHOLDER
<style>
:root{--cy:#5ad7ff;--cy2:#9fe8ff;--dim:#3a6d8a;--amber:#ffb454;--grn:#4ade80;
      --red:#ff6b6b;--ink:#dff3ff}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:radial-gradient(1200px 800px at 50% 42%,#0a1f33 0%,#04101d 55%,#020810 100%);
  color:var(--ink);font:14px/1.45 "SF Mono",Menlo,Consolas,monospace;overflow:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;
  background-image:linear-gradient(rgba(90,215,255,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(90,215,255,.045) 1px,transparent 1px);
  background-size:44px 44px}
body::after{content:"";position:fixed;inset:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.12) 0 2px,transparent 2px 4px)}
.hud{display:grid;height:100vh;padding:20px 30px;gap:12px;
  grid-template-rows:auto auto 1fr 196px;grid-template-columns:280px 1fr 300px;
  grid-template-areas:"top top top" "kpis kpis kpis" "left core right" "feed feed feed"}
.kpis{grid-area:kpis;display:flex;flex-wrap:wrap;gap:6px 30px;
  border-bottom:1px solid rgba(90,215,255,.14);padding:2px 0 10px}
.kpi .label{margin-bottom:1px}
.kpi b{font-size:21px;color:#fff;font-weight:600}
.kpi b.glow{text-shadow:0 0 10px rgba(90,215,255,.6)}
.label{font-size:10px;letter-spacing:.22em;color:var(--dim);text-transform:uppercase}
.glow{text-shadow:0 0 14px rgba(90,215,255,.75),0 0 34px rgba(90,215,255,.30)}
/* top bar */
.top{grid-area:top;display:flex;align-items:baseline;gap:22px;
  border-bottom:1px solid rgba(90,215,255,.22);padding-bottom:12px}
.top .sys{font-size:17px;letter-spacing:.34em;color:var(--cy2)}
.top .greet{color:#8fc7e6;font-size:13px;letter-spacing:.06em}
.top .clock{margin-left:auto;font-size:17px;color:var(--cy2);letter-spacing:.18em}
.chip{font-size:10px;letter-spacing:.2em;padding:4px 12px;border:1px solid;border-radius:3px}
.chip.on{color:var(--grn);border-color:rgba(74,222,128,.5);text-shadow:0 0 10px rgba(74,222,128,.7)}
.chip.off{color:var(--amber);border-color:rgba(255,180,84,.5);text-shadow:0 0 10px rgba(255,180,84,.6)}
/* left stats */
.left{grid-area:left;display:flex;flex-direction:column;justify-content:center;gap:22px}
.stat .label{margin-bottom:4px}
.stat b{display:block;font-size:37px;font-weight:600;color:#fff;line-height:1.05}
.stat .sub{font-size:11px;color:var(--dim);letter-spacing:.08em}
/* core */
.core{grid-area:core;display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:0}
.reactor{width:min(40vh,380px);height:min(40vh,380px);
  filter:drop-shadow(0 0 26px rgba(90,215,255,.35))}
.reactor circle,.reactor line{fill:none;stroke:var(--cy);vector-effect:non-scaling-stroke}
.rSeg{stroke-width:7;stroke-dasharray:52 26;opacity:.85;
  transform-origin:200px 200px;animation:spin 26s linear infinite}
.rSeg2{stroke-width:2.4;stroke-dasharray:8 10;opacity:.7;
  transform-origin:200px 200px;animation:spin 14s linear infinite reverse}
.rThin{stroke-width:1;opacity:.4}
.rSeg3{stroke-width:11;stroke-dasharray:26 42;opacity:.6;
  transform-origin:200px 200px;animation:spin 9s linear infinite}
.spokes line{stroke-width:1.4;opacity:.5}
.spokes{transform-origin:200px 200px;animation:spin 60s linear infinite reverse}
.coreGlow{animation:pulse 2.6s ease-in-out infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:.85}50%{opacity:1}}
.coreLabel{margin-top:16px;text-align:center}
.coreLabel .label{margin-bottom:5px}
.coreLabel b{font-size:15px;letter-spacing:.3em;color:var(--cy2)}
/* right pipeline */
.right{grid-area:right;display:flex;flex-direction:column;justify-content:center;gap:12px}
.bar .label{display:flex;justify-content:space-between;margin-bottom:3px}
.bar .label span:last-child{color:var(--cy2)}
.track{height:7px;background:rgba(90,215,255,.10);border-radius:2px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,rgba(90,215,255,.35),var(--cy));
  box-shadow:0 0 10px rgba(90,215,255,.6);width:0;transition:width .9s ease}
/* feed */
.feed{grid-area:feed;border-top:1px solid rgba(90,215,255,.22);padding-top:10px;
  overflow:hidden}
.feed .label{margin-bottom:8px}
#feedlines{overflow:hidden;font-size:12.5px}
#feedlines div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  padding:1.5px 0;color:#a8d4ea}
#feedlines .t{color:var(--dim)}
#feedlines .k{color:var(--cy2)}
#feedlines .k.pay{color:var(--grn)} #feedlines .k.err{color:var(--red)}
#feedlines .k.warn{color:var(--amber)}
.back{color:var(--dim);text-decoration:none;font-size:11px;letter-spacing:.2em}
.back:hover{color:var(--cy2)}
@media (max-width:900px){
  body{overflow:auto}
  .hud{display:flex;flex-direction:column;height:auto;gap:18px;padding:16px 18px 30px}
  .top{flex-wrap:wrap;row-gap:8px}
  .top .sys{font-size:15px;letter-spacing:.26em}
  .top .clock{margin-left:auto;font-size:15px}
  .top .greet{order:9;flex-basis:100%}
  .back{display:none}
  .kpis{gap:8px 24px;padding-bottom:12px}
  .kpi b{font-size:19px}
  .left{flex-direction:row;flex-wrap:wrap;gap:18px 34px;justify-content:flex-start}
  .stat b{font-size:31px}
  .reactor{width:230px;height:230px}
  .right{gap:10px}
  .feed{padding-bottom:10px}
  #feedlines div{white-space:normal}
}
</style></head><body>
<div class="hud">
  <div class="top">
    <span class="sys glow">J.A.R.V.I.S</span>
    <span class="greet" id="greet"></span>
    <span class="chip" id="autopilot">…</span>
    <span class="clock glow" id="clock">--:--:--</span>
    <a class="back" href="/">◀ CLASSIC</a>
  </div>
  <div class="kpis" id="kpis"></div>
  <div class="left" id="money"></div>
  <div class="core">
    <svg class="reactor" viewBox="0 0 400 400" aria-hidden="true">
      <defs>
        <radialGradient id="cg" cx="50%" cy="50%">
          <stop offset="0%" stop-color="#eaffff" stop-opacity="1"/>
          <stop offset="34%" stop-color="#9fe8ff" stop-opacity=".95"/>
          <stop offset="70%" stop-color="#2ea8dd" stop-opacity=".55"/>
          <stop offset="100%" stop-color="#0a4a6e" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle class="rSeg"  cx="200" cy="200" r="186"/>
      <circle class="rThin" cx="200" cy="200" r="168"/>
      <circle class="rSeg2" cx="200" cy="200" r="150"/>
      <g class="spokes" id="spokes"></g>
      <circle class="rThin" cx="200" cy="200" r="104"/>
      <circle class="rSeg3" cx="200" cy="200" r="82"/>
      <circle class="coreGlow" cx="200" cy="200" r="52" fill="url(#cg)" stroke="none"/>
    </svg>
    <div class="coreLabel">
      <div class="label">Solo Studio pipeline core</div>
      <b class="glow" id="coreState">SYSTEMS NOMINAL</b>
    </div>
  </div>
  <div class="right" id="bars"></div>
  <div class="feed">
    <div class="label">Mission log — live</div>
    <div id="feedlines"></div>
  </div>
</div>
<script>
(function(){
  var spokes = document.getElementById('spokes');
  for (var i = 0; i < 12; i++) {
    var a = i * Math.PI / 6;
    var l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l.setAttribute('x1', 200 + 108 * Math.cos(a)); l.setAttribute('y1', 200 + 108 * Math.sin(a));
    l.setAttribute('x2', 200 + 146 * Math.cos(a)); l.setAttribute('y2', 200 + 146 * Math.sin(a));
    spokes.appendChild(l);
  }
  function pad(n){ return (n < 10 ? '0' : '') + n; }
  function tickClock(){
    var d = new Date();
    document.getElementById('clock').textContent =
      pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  setInterval(tickClock, 1000); tickClock();

  var STAGE_LABELS = {found:'FOUND', contacted:'CONTACTED', building_preview:'BUILDING',
    preview_sent:'PREVIEW SENT', sending_payment_link:'SENDING LINK',
    payment_link_sent:'AWAITING PAYMENT', paid:'PAID', deploying_final:'DEPLOYING',
    delivered:'DELIVERED', not_interested:'PASSED', error:'ATTENTION'};

  function render(d){
    var greetWord = 'Good evening'; var h = new Date().getHours();
    if (h >= 5 && h < 12) greetWord = 'Good morning';
    else if (h >= 12 && h < 18) greetWord = 'Good afternoon';
    document.getElementById('greet').textContent =
      greetWord + (d.owner ? ', ' + d.owner : '') + '. All services standing by.';
    var ap = document.getElementById('autopilot');
    ap.textContent = d.autopilot ? 'AUTOPILOT · ONLINE' : 'AUTOPILOT · STANDBY';
    ap.className = 'chip ' + (d.autopilot ? 'on' : 'off');
    var money = document.getElementById('money'); money.textContent = '';
    d.money.forEach(function(m){
      var w = document.createElement('div'); w.className = 'stat';
      var lab = document.createElement('div'); lab.className = 'label'; lab.textContent = m.l;
      var b = document.createElement('b'); b.className = 'glow'; b.textContent = m.v;
      var sub = document.createElement('div'); sub.className = 'sub'; sub.textContent = m.s || '';
      w.appendChild(lab); w.appendChild(b); w.appendChild(sub); money.appendChild(w);
    });
    var kpis = document.getElementById('kpis'); kpis.textContent = '';
    d.kpis.forEach(function(m){
      var w = document.createElement('div'); w.className = 'kpi';
      var lab = document.createElement('div'); lab.className = 'label'; lab.textContent = m.l;
      var b = document.createElement('b'); if (m.hot) b.className = 'glow'; b.textContent = m.v;
      w.appendChild(lab); w.appendChild(b); kpis.appendChild(w);
    });
    document.getElementById('coreState').textContent =
      d.attention > 0 ? d.attention + ' ITEM' + (d.attention === 1 ? '' : 'S') + ' NEED YOU'
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
      (function(f, w){ requestAnimationFrame(function(){ f.style.width = w + '%'; }); })
        (fill, Math.round(100 * d.stages[k] / max));
    }
    var feed = document.getElementById('feedlines'); feed.textContent = '';
    if (!d.events.length) {
      var e0 = document.createElement('div');
      e0.textContent = '[--:--:--] awaiting first mission — find leads from the classic view';
      feed.appendChild(e0);
    }
    d.events.forEach(function(ev){
      var line = document.createElement('div');
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
  }
  function refresh(){
    fetch('/jarvis/data').then(function(r){ return r.json(); }).then(render)
      .catch(function(){ document.getElementById('coreState').textContent = 'LINK LOST — RETRYING'; });
  }
  setInterval(refresh, 4000); refresh();
})();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"app": HEALTH_MARKER}


def _live_snapshot() -> dict:
    db = STATE.db
    latest = db.recent_events(1)
    return {
        "pending": len(db.leads_awaiting_approval()),
        "attention": len(db.attention_events()),
        "last_event": latest[0]["id"] if latest else 0,
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
        "background_color": "#04101d", "theme_color": "#04101d",
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
<style>body{background:#04101d;color:#dff3ff;font:16px -apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{text-align:center;padding:24px}
h1{font-size:20px;letter-spacing:.3em;color:#9fe8ff;font-weight:600}
input{font-size:28px;text-align:center;letter-spacing:.4em;width:220px;padding:12px;
border-radius:10px;border:1px solid #2a5a7a;background:#0a1f33;color:#fff;margin:18px 0}
button{font-size:16px;font-weight:600;padding:12px 40px;border-radius:10px;border:0;
background:#2563eb;color:#fff}
.err{color:#ff8f8f;min-height:1.4em}</style></head>
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
<style>body{background:#04101d;color:#dff3ff;font:16px -apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{text-align:center;padding:24px;width:100%;max-width:340px}
h1{font-size:20px;letter-spacing:.3em;color:#9fe8ff;font-weight:600}
input{font-size:18px;width:100%;padding:14px;border-radius:10px;
border:1px solid #2a5a7a;background:#0a1f33;color:#fff;margin:18px 0}
button{font-size:16px;font-weight:600;padding:14px 40px;border-radius:10px;
border:0;background:#2563eb;color:#fff;width:100%}
.err{color:#ff8f8f;min-height:1.4em;font-size:14px}</style></head>
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
    return _render(DASHBOARD, leads=leads,
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
  <div style="border:1px solid var(--line);border-radius:8px;padding:12px;
    background:#fbfcfe;margin:10px 0">
    <div style="font-weight:600;margin-bottom:6px">{{ item.rendered.subject }}</div>
    <div style="white-space:pre-wrap;font-size:13.5px;color:#33414f">{{ item.rendered.body }}</div>
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
<p class="muted">Google doesn't publish business emails. Look these up (Facebook,
Yelp, or a quick call) and paste the address — they'll move up to the approval
list above.</p>
<div class="tablewrap"><table class="stack"><tbody>
{% for l in needs_email %}
<tr>
  <td><b>{{ l['name'] }}</b><div class="muted">{{ l['category'] or '' }}
      {% if l['phone'] %}· {{ l['phone'] }}{% endif %}</div></td>
  <td style="min-width:210px">
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


KEY_FIELDS = [
    ("google_places_api_key", "Google Places API key", "AIza…"),
    ("inkbox_api_key", "Inkbox API key", ""),
    ("anthropic_api_key", "Anthropic (Claude) API key", "sk-ant-…"),
    ("netlify_api_key", "Netlify personal access token", "nfp_…"),
    ("stripe_secret_key", "Stripe secret key", "sk_test_… or sk_live_…"),
]


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        cfg = core.load_config()
        for field, _label, _hint in KEY_FIELDS:
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
        if "saved_searches" in request.form:
            cfg["saved_searches"] = request.form.get("saved_searches", "")
        for field, lo, hi in (("search_interval_hours", 1, 168),
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
    return _render(SETUP, key_fields=KEY_FIELDS, lan_ip=ip,
                   phone_qr=qr_svg(target))


@app.get("/setup/test_notification")
def test_notification():
    try:
        STATE.services.push_notify(
            "Solo Studio test", "Notifications are working! You'll get a buzz "
            "for replies, previews, and payments.", tags="white_check_mark")
        flash("Test notification sent — check your phone (subscribe to the topic "
              "in the ntfy app first).", "ok")
    except Exception as e:
        flash(f"Couldn't send: {e}", "err")
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
            results.append((name, False, str(e)[:300]))

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
    host = "0.0.0.0" if (cfg.get("phone_access_enabled")
                         and cfg.get("phone_pin")) else "127.0.0.1"
    if host == "0.0.0.0":
        print(f"Phone access ON — from your phone: http://{lan_ip()}:{args.port}/")
    print(f"Solo Studio dashboard: {url}")
    app.run(host=host, port=args.port, debug=False, use_reloader=False)


if CLOUD_MODE:
    # Under a WSGI server main() never runs, so start the worker at import.
    start_autopilot_thread()


if __name__ == "__main__":
    main()
