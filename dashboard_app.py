"""Solo Studio dashboard — Flask web UI wrapping solo_studio_agent.

Run:  python3 dashboard_app.py [--open-browser]

Everything is configured on the Setup page (saved to config.json) — no code
editing, no environment variables. The dashboard binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import webbrowser

from flask import (Flask, abort, flash, redirect, render_template_string,
                   request, url_for)

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

app = Flask(__name__)
app.secret_key = os.urandom(24)


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


def start_autopilot_thread():
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
@media (max-width:800px){.grid{grid-template-columns:1fr}}
</style></head>
<body>
<header>
  <span class="brand">Solo Studio</span>
  <a href="{{ url_for('dashboard') }}">Dashboard</a>
  <a href="{{ url_for('activity') }}">Activity</a>
  <a href="{{ url_for('setup') }}">Setup</a>
</header>
<main>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, m in messages %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% block body %}{% endblock %}
</main>
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
phone call) before outreach can go out.</p>
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
    return {"config": STATE.config}


def _render(tpl, **ctx):
    return render_template_string(tpl, **ctx)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"app": HEALTH_MARKER}


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
        core.save_config(cfg)
        STATE.reload()
        flash("Settings saved.", "ok")
        return redirect(url_for("setup"))
    return _render(SETUP, key_fields=KEY_FIELDS)


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
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

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
    print(f"Solo Studio dashboard: {url}")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
