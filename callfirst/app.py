"""Call First — the dashboard.

One screen matters here: the call sheet. Everything else is either feeding it
or cleaning up after it.

The app this grew out of tried to reach local businesses by cold email. It
could not, and the reason was structural rather than a bug: the best prospect
is a business with no website, and a business with no website has no email
address anywhere online either. The queue sat at zero for weeks.

So the order is reversed. The app finds businesses and puts a phone number in
front of you. You ring them. They say yes and read you an email address. From
that moment everything is automatic again — preview built, preview sent,
payment link, and the finished site deployed once Stripe says the money landed.

Nothing in here ever sends a cold email. Every address it writes to was said
out loud to you by the person who owns it.
"""

import argparse
import os
import socket
import sys
import threading
import time
import traceback

from flask import (Flask, abort, flash, redirect, render_template,
                   request, session, url_for)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as core  # noqa: E402

PORT = 8748                       # deliberately not the old app's 8747
HEALTH_MARKER = "call-first"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reload()

    def reload(self):
        self.config = core.load_config()
        # Keep the open database handle across a reload — worker threads hold
        # connections to it — but only while it is still the right file.
        db = getattr(self, "db", None)
        wanted = os.path.join(core.app_data_dir(), "solo_studio.db")
        self.db = db if db is not None and db.path == wanted else core.Database()
        self.services = core.Services(self.config)
        self.agent = core.Agent(self.db, self.services, self.config)


STATE = State()

CLOUD_PASSWORD = os.environ.get("CALL_FIRST_PASSWORD", "").strip()
CLOUD_MODE = bool(CLOUD_PASSWORD)
BOUND_HOST = "127.0.0.1"

app = Flask(__name__)
app.secret_key = os.environ.get("CALL_FIRST_SECRET") or os.urandom(24).hex()

OPEN_ENDPOINTS = {"login", "pin", "health", "static"}


def _is_local_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


@app.before_request
def _access_gate():
    """Local requests pass freely; anything else needs the PIN.

    In cloud mode every request needs the password, with no local bypass —
    behind a hosting proxy a request from anywhere can look local.
    """
    if CLOUD_MODE:
        if request.endpoint in OPEN_ENDPOINTS or session.get("cloud_ok"):
            return None
        return redirect(url_for("login"))
    if _is_local_request():
        return None
    cfg = STATE.config
    if not cfg.get("phone_access_enabled") or not cfg.get("phone_pin"):
        abort(403)
    if request.endpoint in OPEN_ENDPOINTS or session.get("phone_ok"):
        return None
    return redirect(url_for("pin"))


# ---------------------------------------------------------------------------
# One background job at a time, so a slow search can't block the page
# ---------------------------------------------------------------------------

JOB = {"running": False, "label": "", "summary": ""}


def _start_job(label, work) -> bool:
    if JOB["running"]:
        return False
    JOB.update(running=True, label=label, summary="")

    def run():
        try:
            JOB["summary"] = work() or ""
        except Exception as e:
            JOB["summary"] = core.explain(e, 300)
            STATE.db.log(None, "job_failed", f"{label}: {core.explain(e, 300)}",
                         needs_attention=True)
        finally:
            JOB["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def _worker_loop():
    """The background round. Errors here are logged, never fatal — a worker
    that dies silently is how the last app looked broken for days."""
    while True:
        try:
            STATE.db.set_kv("worker_beat", core._now())
            STATE.agent.tick()
        except Exception:
            try:
                STATE.db.log(None, "worker_error", traceback.format_exc()[-400:],
                             needs_attention=True)
            except Exception:
                pass
        time.sleep(max(20, core.setting_int(STATE.config,
                                            "poll_interval_seconds", 60)))


# ---------------------------------------------------------------------------
# Template context every page needs
# ---------------------------------------------------------------------------

@app.context_processor
def _chrome():
    try:
        calls = len(STATE.db.leads_to_call())
        live = len([l for l in STATE.db.all_leads()
                    if l["stage"] in LIVE_STAGES])
        attention = len(STATE.db.attention_events())
        last = STATE.db.recent_events(1)
        stamp = "%s:%d:%d" % (last[0]["id"] if last else 0, calls, attention)
    except Exception:
        calls = live = attention = 0
        stamp = "0:0:0"
    return {
        "studio_name": (STATE.config.get("studio_name") or "Call First").strip(),
        "call_count": calls, "live_count": live, "attention": attention,
        "job": JOB, "live_stamp": stamp, "cloud_mode": CLOUD_MODE,
    }


LIVE_STAGES = (core.STAGE_BUILDING_PREVIEW, core.STAGE_PREVIEW_SENT,
               core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT,
               core.STAGE_PAID, core.STAGE_DEPLOYING_FINAL)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

CALL_SHEET = """
{% extends "base" %}{% block body %}

{% if not ready.can_find %}
<div class="warnbar"><div>
  <b>Set your area first.</b> {{ ready.why }}
</div><a class="btn" href="{{ url_for('setup') }}">Open Setup</a></div>
{% endif %}

{% if not sheet %}
<div class="card">
<h1>Nobody to call yet</h1>
<p class="muted">The sheet fills itself once an area is set — and tops itself
up whenever it runs low, so it shouldn't empty again.</p>
<form method="post" action="{{ url_for('find_now') }}">
  <button class="btn btn-primary" {% if not ready.can_find %}disabled
    title="{{ ready.why }}"{% endif %}>Find businesses to call</button></form>
</div>
{% else %}

<div class="card">
<h1>Call {{ next.name }}</h1>
<p class="muted" style="margin-top:-6px">{{ next.category or 'Local business' }}
{% if next.address %} · {{ next.address }}{% endif %}</p>

<a class="bigdial" href="tel:{{ next.phone }}">📞 {{ next.phone }}</a>

<div class="sitebox">
  <div><span class="k">Their website</span>
    <b>{{ site_words(next) }}</b></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
    {% if next.website_url %}
    <a class="btn btn-sm" href="{{ next.website_url }}" target="_blank"
       rel="noopener noreferrer">Look at their site ↗</a>{% endif %}
    {% if next.maps_url %}
    <a class="btn btn-sm" href="{{ next.maps_url }}" target="_blank"
       rel="noopener noreferrer">Their listing ↗</a>{% endif %}
  </div>
</div>

<details open style="margin:14px 0">
  <summary class="muted">What to say</summary>
  <div class="emailbox"><div class="body">{{ opener }}</div></div>
</details>

{% if next.call_notes %}
<div class="note info" style="margin-bottom:12px">
  <div class="k">Last time you rang</div>
  <p class="muted" style="margin:5px 0 0">{{ next.call_notes }}</p></div>
{% endif %}

<h2 style="margin-top:18px">How did it go?</h2>

<form method="post" action="{{ url_for('said_yes', lead_id=next.id) }}"
      class="yesbox">
  <b>They said yes — what email did they give you?</b>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
    <input type="text" name="email" placeholder="email@theirbusiness.com"
      inputmode="email" autocapitalize="off" autocorrect="off"
      style="flex:1;min-width:230px" required>
    <button class="btn btn-primary">Build their site &amp; send it</button>
  </div>
  <p class="muted" style="margin:8px 0 0">Read it back to them before you hang
  up. It's the only way to reach them now — and the preview goes out as soon as
  you press this.</p>
</form>

<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px">
  <form method="post" action="{{ url_for('called', lead_id=next.id) }}">
    <input type="hidden" name="outcome" value="call_back">
    <input type="hidden" name="hours" value="4">
    <button class="btn">No answer — try again later</button></form>
  <form method="post" action="{{ url_for('called', lead_id=next.id) }}">
    <input type="hidden" name="outcome" value="call_back">
    <input type="hidden" name="hours" value="168">
    <button class="btn">Call back next week</button></form>
  <form method="post" action="{{ url_for('called', lead_id=next.id) }}">
    <input type="hidden" name="outcome" value="not_interested">
    <button class="btn btn-danger">Not interested</button></form>
</div>
</div>

<div class="card">
<h2>Next up ({{ sheet|length - 1 }} more)</h2>
<div class="tablewrap"><table class="stack"><tbody>
{% for l in sheet[1:26] %}
<tr>
  <td><b>{{ l['name'] }}</b><div class="muted">{{ l['category'] or '' }}
    · {{ site_words(l) }}</div></td>
  <td style="white-space:nowrap"><a href="tel:{{ l['phone'] }}">{{ l['phone'] }}</a></td>
</tr>
{% endfor %}
</tbody></table></div>
<form method="post" action="{{ url_for('find_now') }}" style="margin-top:12px">
  <button class="btn" {% if not ready.can_find %}disabled
    title="{{ ready.why }}"{% endif %}>Find more</button></form>
</div>
{% endif %}

{% endblock %}
"""

PIPELINE_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>In progress</h1>
<p class="muted" style="margin-top:-6px">Everything past the phone call. This
all runs on its own — nothing here is waiting on you.</p>
<div class="statrow">
  <div class="stat"><b>{{ counts.building }}</b><span>Building</span></div>
  <div class="stat"><b>{{ counts.preview_sent }}</b><span>Preview sent</span></div>
  <div class="stat"><b>{{ counts.awaiting_payment }}</b><span>Awaiting payment</span></div>
  <div class="stat"><b>{{ counts.delivered }}</b><span>Delivered</span></div>
  <div class="stat"><b>${{ revenue }}</b><span>Paid</span></div>
</div>
</div>

{% if rows %}
<div class="card">
<div class="tablewrap"><table><thead><tr>
  <th>Business</th><th>Stage</th><th>Email</th><th>Site</th></tr></thead><tbody>
{% for l in rows %}
<tr>
  <td><b>{{ l['name'] }}</b>{% if l['error'] %}
    <div class="muted" style="color:var(--bad)">{{ l['error'][:120] }}</div>
    {% endif %}</td>
  <td><span class="badge b-{{ l['stage'] }}">{{ l['stage'].replace('_',' ') }}</span></td>
  <td class="muted">{{ l['email'] or '—' }}</td>
  <td>{% if l['netlify_url'] %}<a href="{{ l['netlify_url'] }}" target="_blank"
    rel="noopener noreferrer">open ↗</a>{% else %}—{% endif %}</td>
</tr>
{% endfor %}
</tbody></table></div>
</div>
{% else %}
<div class="card"><p class="muted">Nothing in flight. Go and ring someone.</p></div>
{% endif %}
{% endblock %}
"""

SETUP_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Setup</h1>
{% for f in findings %}
<div class="note {{ 'warn' if f.level == 'fix' else 'info' }}"
     style="margin-bottom:10px">
  <div class="k">{{ f.title }}</div>
  <p class="muted" style="margin:5px 0 0">{{ f.detail }}</p></div>
{% endfor %}

<form method="post" action="{{ url_for('save_setup') }}">
<div class="grid">
<div>
  <h2>You</h2>
  <label>Your name</label>
  <input type="text" name="your_name" value="{{ cfg.your_name or '' }}">
  <label>Studio name</label>
  <input type="text" name="studio_name" value="{{ cfg.studio_name or '' }}">
  <label>Price per site (USD)</label>
  <input type="number" name="site_price_usd" value="{{ cfg.site_price_usd or 500 }}">

  <h2 style="margin-top:20px">Where to look</h2>
  <label>Your area</label>
  <input type="text" name="territory_base" placeholder="Napanoch, NY"
    value="{{ cfg.territory_base or '' }}">
  <label>Kinds of business <span class="muted">(one per line; blank = a sensible default list)</span></label>
  <textarea name="trades" placeholder="plumbers&#10;electricians&#10;roofers"
    >{{ cfg.trades or '' }}</textarea>
</div>
<div>
  <h2>Keys</h2>
  <p class="muted">Finding businesses needs the Google key. Building and
  sending a site needs the other three — but only after someone says yes, so
  you can start calling with just the first one.</p>
  <label>Google Places API key <span class="muted">(5,000 free lookups a month)</span></label>
  <input type="password" name="google_places_api_key"
    value="{{ cfg.google_places_api_key or '' }}">
  <label>Anthropic API key <span class="muted">(designs the site)</span></label>
  <input type="password" name="anthropic_api_key"
    value="{{ cfg.anthropic_api_key or '' }}">
  <label>Netlify API key <span class="muted">(puts the site online)</span></label>
  <input type="password" name="netlify_api_key"
    value="{{ cfg.netlify_api_key or '' }}">
  <label>Inkbox API key <span class="muted">(sends the email)</span></label>
  <input type="password" name="inkbox_api_key"
    value="{{ cfg.inkbox_api_key or '' }}">
  <label>Stripe secret key <span class="muted">(takes the money)</span></label>
  <input type="password" name="stripe_api_key"
    value="{{ cfg.stripe_api_key or '' }}">
</div>
</div>
<div style="margin-top:18px"><button class="btn btn-primary">Save</button></div>
</form>
</div>

<div class="card">
<h2>Spending</h2>
<p class="muted">Google has billed you for <b>{{ google_calls }}</b> lookups
this month out of <b>{{ google_free }}</b> free ones. This app refuses to go
past {{ google_cap }}, so it cannot run up a bill on its own.</p>
<p class="muted">Claude is only ever asked to design a site <em>after</em>
somebody agrees on the phone — about a dollar each, and never on spec.</p>
</div>
{% endblock %}
"""

ACTIVITY_PAGE = """
{% extends "base" %}{% block body %}
<div class="card">
<h1>Activity</h1>
<div class="tablewrap"><table><thead><tr>
  <th>When</th><th>What</th><th>Detail</th></tr></thead><tbody>
{% for e in events %}
<tr {% if e['needs_attention'] and not e['resolved'] %}class="attention"{% endif %}>
  <td class="muted" style="white-space:nowrap">{{ e['created_at'][:16].replace('T',' ') }}</td>
  <td>{{ e['kind'].replace('_',' ') }}</td>
  <td class="muted">{{ e['detail'] }}</td>
</tr>
{% endfor %}
</tbody></table></div>
</div>
{% endblock %}
"""

PIN_PAGE = """
{% extends "base" %}{% block body %}
<div class="card" style="max-width:340px;margin:60px auto">
<h1>PIN</h1>
<form method="post"><input type="password" name="pin" inputmode="numeric"
  autofocus><button class="btn btn-primary" style="margin-top:12px">Unlock</button>
</form></div>
{% endblock %}
"""

LOGIN_PAGE = """
{% extends "base" %}{% block body %}
<div class="card" style="max-width:340px;margin:60px auto">
<h1>Password</h1>
<form method="post"><input type="password" name="password" autofocus>
<button class="btn btn-primary" style="margin-top:12px">Sign in</button>
</form></div>
{% endblock %}
"""


def site_words(lead) -> str:
    """What to say about their website, in a phrase rather than a code."""
    status = (lead["site_status"] if not isinstance(lead, dict)
              else lead.get("site_status")) or ""
    return core.SITE_REASON.get(status, "Not checked")


def _ready() -> dict:
    """Can this app go and find businesses right now, and if not, why not."""
    cfg = STATE.config
    if not (cfg.get("territory_base") or "").strip():
        return {"can_find": False,
                "why": "Put your town in Setup and the sheet fills itself."}
    if not (cfg.get("google_places_api_key") or "").strip():
        return {"can_find": True,
                "why": "No Google key yet — it'll fall back to OpenStreetMap, "
                       "which is free but thinner."}
    return {"can_find": True, "why": ""}


@app.route("/")
def call_sheet():
    sheet = STATE.db.leads_to_call()
    nxt = dict(sheet[0]) if sheet else None
    return render_template("call_sheet", sheet=sheet, next=nxt,
                           opener=core.call_opener(nxt or {}, STATE.config)
                           if nxt else "",
                           site_words=site_words, ready=_ready())


@app.route("/pipeline")
def pipeline():
    leads = STATE.db.all_leads()
    rows = [l for l in leads if l["stage"] in LIVE_STAGES
            or l["stage"] == core.STAGE_DELIVERED]
    counts = {
        "building": sum(1 for l in leads
                        if l["stage"] == core.STAGE_BUILDING_PREVIEW),
        "preview_sent": sum(1 for l in leads
                            if l["stage"] == core.STAGE_PREVIEW_SENT),
        "awaiting_payment": sum(1 for l in leads if l["stage"] in (
            core.STAGE_SENDING_PAYMENT_LINK, core.STAGE_PAYMENT_LINK_SENT)),
        "delivered": sum(1 for l in leads
                         if l["stage"] == core.STAGE_DELIVERED),
    }
    return render_template("pipeline", rows=rows, counts=counts,
                           revenue=STATE.db.revenue_cents() // 100)


@app.route("/setup")
def setup():
    cfg = dict(STATE.config)
    return render_template(
        "setup", cfg=cfg, findings=core.checkup(STATE.db, STATE.config),
        google_calls=STATE.agent.google_calls_this_month(),
        google_free=core.GOOGLE_FREE_CALLS_MONTH,
        google_cap=core.setting_int(STATE.config, "monthly_google_cap",
                                    core.GOOGLE_CALL_CAP))


@app.route("/activity")
def activity():
    return render_template("activity", events=STATE.db.recent_events(120))


@app.route("/live")
def live():
    last = STATE.db.recent_events(1)
    return {"last_event": last[0]["id"] if last else 0,
            "pending": len(STATE.db.leads_to_call()),
            "attention": len(STATE.db.attention_events())}


@app.route("/health")
def health():
    return HEALTH_MARKER


# -- actions ----------------------------------------------------------------

@app.route("/action/find", methods=["POST"])
def find_now():
    if _start_job("finding businesses to call",
                  lambda: _said(STATE.agent.find_businesses())):
        flash("Looking for businesses now — this page updates itself.", "ok")
    else:
        flash(f"Busy — {JOB['label']}.", "err")
    return redirect(url_for("call_sheet"))


def _said(r: dict) -> str:
    if not r.get("ok"):
        return r.get("error") or "That didn't work."
    said = "Added %d to call from %s." % (r.get("added", 0), r.get("area", ""))
    if r.get("troubles"):
        said += " Trouble: " + "; ".join(r["troubles"][:2])
    return said


@app.route("/action/called/<int:lead_id>", methods=["POST"])
def called(lead_id):
    outcome = request.form.get("outcome", "call_back")
    hours = core.setting_int({"h": request.form.get("hours", "4")}, "h", 4)
    r = STATE.agent.mark_called(lead_id, outcome,
                               notes=request.form.get("notes", ""),
                               call_back_hours=hours)
    flash("Noted." if r.get("ok") else r.get("error", "Couldn't save that."),
          "ok" if r.get("ok") else "err")
    return redirect(url_for("call_sheet"))


@app.route("/action/said_yes/<int:lead_id>", methods=["POST"])
def said_yes(lead_id):
    """The pivot of the whole app: a yes on the phone, and an address.

    The build runs in the background because designing a site takes a minute
    and you are still holding a phone.
    """
    email = request.form.get("email", "")
    lead = STATE.db.get_lead(lead_id)
    name = lead["name"] if lead else "them"
    if not core.EMAIL_RE.fullmatch(email.strip()):
        flash("That doesn't look like an email address — read it back to them "
              "and try again.", "err")
        return redirect(url_for("call_sheet"))
    if _start_job(f"building the site for {name}",
                  lambda: _yes_said(STATE.agent.said_yes(lead_id, email))):
        flash(f"Building {name}'s site now — the preview emails itself over "
              "when it's done.", "ok")
    else:
        flash(f"Busy — {JOB['label']}. Try again in a moment.", "err")
    return redirect(url_for("call_sheet"))


def _yes_said(r: dict) -> str:
    return ("Preview built and sent." if r.get("ok")
            else r.get("error") or "Couldn't build that one.")


@app.route("/action/save_setup", methods=["POST"])
def save_setup():
    cfg = core.load_config()
    for key in ("your_name", "studio_name", "territory_base", "trades",
                "google_places_api_key", "anthropic_api_key",
                "netlify_api_key", "inkbox_api_key", "stripe_api_key"):
        if key in request.form:
            cfg[key] = request.form.get(key, "").strip()
    if request.form.get("site_price_usd"):
        cfg["site_price_usd"] = core.setting_int(
            {"p": request.form["site_price_usd"]}, "p", 500)
    core.save_config(cfg)
    STATE.reload()
    flash("Saved.", "ok")
    return redirect(url_for("setup"))


@app.route("/pin", methods=["GET", "POST"])
def pin():
    if request.method == "POST":
        if request.form.get("pin") == str(STATE.config.get("phone_pin")):
            session["phone_ok"] = True
            return redirect(url_for("call_sheet"))
        flash("Wrong PIN.", "err")
    return render_template("pin")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == CLOUD_PASSWORD:
            session["cloud_ok"] = True
            return redirect(url_for("call_sheet"))
        flash("Wrong password.", "err")
    return render_template("login")


BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<!-- Inline so there is no file to serve and no 404 in the log. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Ctext y=%2714%27 font-size=%2714%27%3E%F0%9F%93%9E%3C/text%3E%3C/svg%3E">
<title>{{ studio_name }}</title>
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

/* What has actually gone out. Nothing leaves without approval, so this is a
   record of decisions the owner made, and it should be in plain sight. */
.sendbar{display:flex;gap:16px;flex-wrap:wrap;align-items:baseline;
  margin:0 0 16px;padding:11px 15px;border-radius:var(--r-md);
  background:var(--panel-2);border:1px solid var(--line)}
.sendbar b{font-size:19px}
.sendbar .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;
  margin-right:7px;background:var(--bad)}
.sendbar .pulse.on{background:var(--ok);
  box-shadow:0 0 0 3px rgba(110,231,183,.18)}

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
/* A button that cannot do anything has to look like it. */
.btn:disabled{opacity:.42;cursor:not-allowed}
.btn:disabled:hover{border-color:var(--line);color:var(--ink);
  background:var(--panel)}

/* The phone number is the whole app. On a phone this is the thing your thumb
   goes to, so it is sized for a thumb and nothing sits next to it. */
.bigdial{display:block;text-align:center;margin:16px 0;padding:18px 20px;
  border-radius:var(--r-lg);background:var(--acc);color:var(--acc-ink);
  font-size:29px;font-weight:700;letter-spacing:.01em;text-decoration:none}
.bigdial:hover{background:#f78fc6}
@media (max-width:800px){.bigdial{font-size:26px;padding:20px 14px}}

/* What their website looks like right now — the thing you are ringing about. */
.sitebox{border:1px solid var(--line);border-radius:var(--r-md);
  padding:12px 14px;background:rgba(0,0,0,.2)}
.sitebox .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--acc);font-weight:600;display:block}

/* The one form that spends money and sends mail, so it is visually the one
   thing on the page that looks like a commitment. */
.yesbox{border:1px solid rgba(110,231,183,.34);border-radius:var(--r-md);
  padding:14px 16px;background:rgba(110,231,183,.09)}
/* A button that cannot do anything has to look like it. Hover it for the
   reason — every disabled button here carries one in its title. */
.btn:disabled{opacity:.42;cursor:not-allowed}
.btn:disabled:hover{border-color:var(--line);color:var(--ink);
  background:var(--panel)}
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
  <span class="brand">{{ studio_name }}</span>
  <a href="{{ url_for('call_sheet') }}">Call sheet{% if call_count %}
    <span style="background:var(--acc);color:var(--acc-ink);border-radius:999px;
    padding:1px 7px;font-size:12px;font-weight:700;margin-left:3px"
    >{{ call_count }}</span>{% endif %}</a>
  <a href="{{ url_for('pipeline') }}">In progress{% if live_count %}
    <span style="background:var(--warn);color:#1a1206;border-radius:999px;
    padding:1px 7px;font-size:12px;font-weight:700;margin-left:3px"
    >{{ live_count }}</span>{% endif %}</a>
  <a href="{{ url_for('activity') }}">Activity</a>
  <a href="{{ url_for('setup') }}">Setup</a>
  {% if attention %}<a href="{{ url_for('activity') }}"
    style="margin-left:auto;color:var(--warn)">▲ {{ attention }} to fix</a>
  {% else %}<span style="margin-left:auto"></span>{% endif %}
</header>
<main>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, m in messages %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% if job.running %}
<div class="working"><span class="spin" aria-hidden="true"></span>
  <b>{{ job.label|capitalize }}…</b>
  <span class="muted">This page updates itself when he's done.</span></div>
{% elif job.summary %}
<div class="working done"><span>{{ job.summary }}</span></div>
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

from jinja2 import DictLoader  # noqa: E402

app.jinja_env.loader = DictLoader({
    "base": BASE,
    "call_sheet": CALL_SHEET,
    "pipeline": PIPELINE_PAGE,
    "setup": SETUP_PAGE,
    "activity": ACTIVITY_PAGE,
    "pin": PIN_PAGE,
    "login": LOGIN_PAGE,
})


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((BOUND_HOST, port))
            return True
        except OSError:
            return False


def main():
    global BOUND_HOST
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    BOUND_HOST = args.host

    threading.Thread(target=_worker_loop, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        try:
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    print(f"Call First running at {url}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
