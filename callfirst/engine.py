"""Solo Studio agent — core pipeline logic.

Pipeline: find local businesses with no website (Google Places) -> cold email
(Inkbox) -> on interested reply, deploy a WATERMARKED preview site (Claude
designs it, Netlify hosts it) -> on a second positive reply, send a Stripe
Checkout link -> poll Stripe until payment clears -> deploy the CLEAN final
site and email the live link.

Hard rules enforced here:
  * The clean (final) site is only ever deployed from stage 'paid', and a lead
    only reaches 'paid' when Stripe itself reports payment_status == "paid"
    for the exact Checkout Session we created for that lead.
  * Every stage transition is an atomic SQL "claim" (UPDATE ... WHERE stage=X)
    so duplicate replies, overlapping polls, or a double-clicked button cannot
    run the same step twice.
  * At most one Stripe Checkout Session is active per lead; a new one is only
    created if the previous one expired unpaid.
  * A lead that unsubscribes / declines is flagged do_not_contact and is never
    emailed again.

All configuration lives in config.json (written by the dashboard's Setup
page) — no environment variables, no editing code.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

APP_NAME = "Solo Studio"

# --- self-update -----------------------------------------------------------
# Updated code is written to a folder in Application Support, never into the
# .app bundle: macOS guards app bundles, and editing one breaks its signature.
# The launcher prefers that folder when it holds a valid copy.
UPDATE_REPO = "yy68hpf7vf-ux/solo-studio"
UPDATE_BRANCH = "claude/solo-studio-pipeline-7r9508"
UPDATE_FILES = ("solo_studio_agent.py", "dashboard_app.py", "requirements.txt")
RESTART_EXIT_CODE = 42          # the launcher relaunches on this code


def code_fingerprint(directory: str | None = None) -> str:
    """Short hash of the app's own code, so the launcher can tell whether the
    copy already running is the same one it is about to start."""
    import hashlib
    d = directory or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in ("solo_studio_agent.py", "dashboard_app.py"):
        try:
            with open(os.path.join(d, name), "rb") as f:
                h.update(f.read())
        except OSError:
            return ""
    return h.hexdigest()[:12]


def updates_dir() -> str:
    d = os.path.join(app_data_dir(), "app")
    os.makedirs(d, exist_ok=True)
    return d


def installed_version() -> dict:
    try:
        with open(os.path.join(updates_dir(), "installed.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def check_for_update(timeout: int = 15) -> dict:
    """Ask GitHub what the newest version is. Returns
    {ok, available, sha, short, message, date, installed}."""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/commits/{UPDATE_BRANCH}"
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        return {"ok": False, "error": f"Couldn't reach GitHub: {e}"}
    if resp.status_code != 200:
        return {"ok": False,
                "error": f"GitHub said {resp.status_code}. Try again shortly."}
    data = resp.json()
    sha = data.get("sha", "")
    commit = data.get("commit", {})
    installed = installed_version().get("sha", "")
    return {
        "ok": True,
        "available": bool(sha) and sha != installed,
        "sha": sha,
        "short": sha[:7],
        "message": (commit.get("message") or "").splitlines()[0][:120],
        "date": (commit.get("committer") or {}).get("date", "")[:10],
        "installed": installed[:7],
        "never_updated": not installed,
    }


def apply_update(sha: str | None = None, timeout: int = 60) -> dict:
    """Download the newest code, check it actually parses, and install it.

    Nothing is overwritten until every file has downloaded and compiled, so a
    half-finished download can't leave a broken app behind.
    """
    if sha is None:
        info = check_for_update()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error", "Update check failed.")}
        sha = info["sha"]
    fetched = {}
    for name in UPDATE_FILES:
        url = (f"https://raw.githubusercontent.com/{UPDATE_REPO}/{sha}/{name}")
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Download of {name} failed: {e}"}
        if resp.status_code != 200:
            return {"ok": False,
                    "error": f"Couldn't download {name} (HTTP {resp.status_code})."}
        text = resp.text
        if name.endswith(".py"):
            # Guard against saving an error page or a truncated download.
            try:
                compile(text, name, "exec")
            except SyntaxError as e:
                return {"ok": False,
                        "error": f"The downloaded {name} isn't valid Python "
                                 f"({e}) — update cancelled, nothing changed."}
            if len(text) < 1000:
                return {"ok": False,
                        "error": f"The downloaded {name} looks truncated — "
                                 "update cancelled, nothing changed."}
        fetched[name] = text

    target = updates_dir()
    try:
        for name, text in fetched.items():
            tmp = os.path.join(target, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(target, name))
        with open(os.path.join(target, "installed.json"), "w", encoding="utf-8") as f:
            json.dump({"sha": sha, "applied_at": _now()}, f)
    except OSError as e:
        return {"ok": False, "error": f"Couldn't save the update: {e}"}
    return {"ok": True, "sha": sha, "short": sha[:7]}


# A line a day, for the top of the dashboard.
#
# Most of these are written for this particular job — one person, cold email,
# a lot of silence between the yeses — because a line about *this* is worth
# more on a Tuesday morning than a famous one about something else. The few
# that are quoted are proverbs or are attributed to a source I can point at;
# I would rather print no name than the wrong one.
GOOGLE_FREE_CALLS_MONTH = 5000
RESEARCH_MODEL = "claude-haiku-4-5"
MAIN_MODEL = "claude-opus-5"

# One dial for what the app is allowed to spend on Claude. Finding leads never
# touches it — that is Google and OpenStreetMap — so this is about looking up
# addresses, reading replies, and answering you.
#
# Designing a site is not on the dial at any level: it only runs when somebody
# has asked for one, it is the thing being sold, and it should be the best the
# account can do.
SPEND_LEVELS = {
    "off":    {"lookups_month": 0,   "lookups_day": 0,  "small_model": True,
               "label": "Off — no Claude credit spent on lookups at all"},
    "frugal": {"lookups_month": 50,  "lookups_day": 10, "small_model": True,
               "label": "Frugal — cheap model, a few paid lookups a day"},
    "normal": {"lookups_month": 200, "lookups_day": 25, "small_model": False,
               "label": "Normal — best model for replies, more lookups"},
}
DEFAULT_SPEND = "frugal"
# Two web searches at $10/1,000 plus a small model's tokens on what comes back.
def spend_plan(cfg: dict) -> dict:
    """What this app may spend, and on which model."""
    plan = dict(SPEND_LEVELS.get(cfg.get("spend_level") or DEFAULT_SPEND,
                                 SPEND_LEVELS[DEFAULT_SPEND]))
    # An explicit cap still wins — the dial sets it, it doesn't lock it.
    if cfg.get("monthly_lookup_cap") not in (None, ""):
        plan["lookups_month"] = max(0, setting_int(cfg, "monthly_lookup_cap",
                                                   plan["lookups_month"]))
    return plan


def thinking_model(cfg: dict) -> str:
    """The model for work that is judgement, not extraction: reading a reply,
    answering a question. Small when the dial says to be frugal."""
    if spend_plan(cfg)["small_model"]:
        return (cfg.get("research_model") or "").strip() or RESEARCH_MODEL
    return (cfg.get("anthropic_model") or "").strip() or MAIN_MODEL
def _web_search_tool_for(model: str) -> str:
    """The web-search tool variant a model actually accepts.

    The newer one needs Opus 4.6+ or Sonnet 4.6+; Haiku takes the basic one,
    and sending the wrong variant is a 400 rather than a graceful fallback.
    """
    modern = ("claude-opus-", "claude-sonnet-5", "claude-sonnet-4-6",
              "claude-fable-", "claude-mythos-")
    return ("web_search_20260209" if model.startswith(modern)
            else "web_search_20250305")


def setting_int(cfg: dict, key: str, default: int) -> int:
    """A whole-number setting, where zero means zero.

    The obvious `cfg.get(k, d) or d` turns a deliberate 0 into the default,
    which for a spending cap is the opposite of what was asked for.
    """
    value = cfg.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _google_meter_key() -> str:
    return "google_calls_" + datetime.now(timezone.utc).strftime("%Y-%m")


# Every city JARVIS works through, largest first within each state. Names
# only: a name is something to be sure of, whereas coordinates typed from
# memory are a wrong search that costs money. Each one is looked up on the map
# once, when the crawl reaches it, and remembered forever after.
DEFAULT_TRADES = [
    "plumbers", "electricians", "landscapers", "tree service", "roofers",
    "handyman", "house cleaning", "towing", "junk removal", "septic service",
    "masonry", "excavation", "snow plowing", "small engine repair",
    "auto repair", "barber shops", "moving companies", "pest control",
    "HVAC", "fencing contractors",
]


def trade_list(text) -> list[str]:
    """The kinds of business to look for, however they were written down.

    The Setup box is free text — commas, new lines, or left empty — so this
    accepts all three and falls back to the built-in list rather than
    searching for nothing.
    """
    if isinstance(text, (list, tuple)):
        items = [str(t).strip() for t in text]
    else:
        items = [t.strip() for t in
                 (text or "").replace(",", "\n").splitlines()]
    return [t for t in items if t] or list(DEFAULT_TRADES)


def fmt_price(value) -> str:
    """500.0 -> '500', 499.5 -> '499.50' (for email/UI copy)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

# Google Places bills nothing for the first 5,000 calls a month. One person
# can call maybe 1,000 businesses a month, and one call here returns up to
# 20 of them — so the free tier is several times more than this app can use.
# The cap sits just under it so a runaway loop can never produce a bill.
GOOGLE_CALL_CAP = 4500

STAGE_FOUND = "found"                      # discovered via Places, may lack email
STAGE_CONTACTED = "contacted"              # cold email sent, waiting for reply
STAGE_BUILDING_PREVIEW = "building_preview"    # transient: generating + deploying preview
STAGE_PREVIEW_SENT = "preview_sent"        # watermarked preview emailed
STAGE_SENDING_PAYMENT_LINK = "sending_payment_link"  # transient
STAGE_PAYMENT_LINK_SENT = "payment_link_sent"  # checkout link emailed, polling Stripe
STAGE_PAID = "paid"                        # Stripe verified paid; delivery pending
STAGE_DEPLOYING_FINAL = "deploying_final"  # transient: deploying clean site
STAGE_DELIVERED = "delivered"              # clean site live + link emailed
STAGE_CALL_BACK = "call_back"              # rang, no answer / try again later
STAGE_NOT_INTERESTED = "not_interested"    # declined or unsubscribed
STAGE_ERROR = "error"                      # gave up after repeated failures

ALL_STAGES = [
    STAGE_FOUND, STAGE_CONTACTED, STAGE_BUILDING_PREVIEW, STAGE_PREVIEW_SENT,
    STAGE_SENDING_PAYMENT_LINK, STAGE_PAYMENT_LINK_SENT, STAGE_PAID,
    STAGE_DEPLOYING_FINAL, STAGE_DELIVERED, STAGE_NOT_INTERESTED, STAGE_ERROR,
]

# Transient stages resume automatically each tick (idempotently).
TRANSIENT_STAGES = {
    STAGE_BUILDING_PREVIEW, STAGE_SENDING_PAYMENT_LINK, STAGE_DEPLOYING_FINAL,
}

MAX_ATTEMPTS = 5  # per transient stage before parking the lead in 'error'


# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

def app_data_dir() -> str:
    """Directory for config.json, the database, and logs.

    Resolution order:
      1. SOLO_STUDIO_HOME env var (used by tests; not required in normal use)
      2. a config.json sitting next to this file (portable/dev layout)
      3. ~/Library/Application Support/Solo Studio on macOS, ~/.solo-studio elsewhere
    """
    env = os.environ.get("SOLO_STUDIO_HOME")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "config.json")):
        return here
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Solo Studio")
    else:
        d = os.path.expanduser("~/.solo-studio")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(app_data_dir(), "config.json")


DEFAULT_CONFIG = {
    # API keys — all filled in via the dashboard Setup page.
    "google_places_api_key": "",
    "inkbox_api_key": "",
    "inkbox_agent_handle": "",
    "anthropic_api_key": "",
    "anthropic_model": "claude-opus-5",
    "netlify_api_key": "",
    # Optional extras. Everything works without them; each one widens the net.
    "stripe_secret_key": "",
    # Business / outreach settings.
    "your_name": "",
    "studio_name": "Solo Studio",
    "mailing_address": "",       # physical address, required in cold email (CAN-SPAM)
    "site_price_usd": 500,
    "currency": "usd",
    "outreach_subject": "A website for {lead_name}",
    "outreach_body": (
        "Hi,\n\n"
        "I came across {lead_name} and noticed you don't seem to have a website yet. "
        "I'm {your_name}, I run {studio_name}, a small local web design studio.\n\n"
        "I build simple, professional one-page websites for local businesses for a flat "
        "${price} — no subscriptions, no hidden fees. If you're interested, just reply to "
        "this email and I'll design a free preview of what your site could look like. "
        "You only pay if you love it.\n\n"
        "Best,\n{your_name}\n{studio_name}\n{mailing_address}\n\n"
        "If you'd rather not hear from me again, reply with the word \"unsubscribe\" "
        "and I won't contact you again."
    ),
    # Behavior.
    # On by default. An app whose whole point is doing the work on its own
    # should not need two switches found and flipped first — being off, and
    # saying nothing about it, is how it sat silent.
    "automation_on_by_default": False,   # flipped true by the migration below
    "autopilot_enabled": True,   # background processing of replies/payments
    "poll_interval_seconds": 60,
    # Automatic prospecting: the agent runs these searches on a schedule and
    # queues what it finds for your approval. It never emails anyone on its own.
    "auto_search_enabled": True,
    "auto_research_enabled": True,    # let the Researcher hunt missing emails
    "research_per_tick": 8,           # PAID lookups attempted each round
    "free_lookups_per_round": 60,     # free page reads each round — no budget
    # An address the Researcher found goes straight onto the lead instead of
    # Keep the call sheet stocked: when fewer than this many businesses are
    # left to ring, go and find more. Discovery is cheap and bounded by the
    # Google meter below, so there is no reason to make you press a button.
    "call_list_floor": 25,
    "dry_area_rest_hours": 6,       # wait this long after a search that found nothing new
    "find_radius_m": 8000,
    "monthly_google_cap": GOOGLE_CALL_CAP,
    "research_model": RESEARCH_MODEL,   # extraction, not reasoning
    "spend_level": DEFAULT_SPEND,       # off | frugal | normal
    "monthly_lookup_cap": None,         # blank means "whatever the dial says"
    "daily_lookup_cap": None,
    "lookup_searches": 2,               # web searches per paid lookup
    "saved_searches": "",         # one search per line
    "search_interval_hours": 12,
    # 20 searches x 3 pages, twice a day, is ~3,600 Google calls a month —
    # inside the 5,000 free ones, and enough ground per run to actually turn
    # something up. The Setup page projects the cost of any other number.
    "searches_per_run": 20,
    "territory_base": "",         # e.g. "Napanoch, NY"
    "trades": "",                 # kinds of business; blank means the built-in list
    "territory_miles": 30,
    "daily_send_cap": 20,         # max approved cold emails sent per day
    # Phone access (dashboard reachable from your phone on the same Wi-Fi).
    "phone_access_enabled": False,
    "phone_pin": "",
    # Push notifications to your phone via the free ntfy.sh service.
    "ntfy_enabled": False,
    "ntfy_topic": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update(stored)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if not cfg.get("automation_on_by_default"):
        # Anyone set up before automation was the default has both switches
        # saved as off, which is why nothing appeared to happen. Turn them on
        # once, and record that it's been done so a deliberate "off" sticks.
        cfg["autopilot_enabled"] = True
        cfg["auto_search_enabled"] = True
        cfg["automation_on_by_default"] = True
        try:
            save_config(cfg)
        except OSError:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # holds API keys
    except OSError:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    place_id TEXT UNIQUE,
    name TEXT NOT NULL,
    address TEXT,
    phone TEXT,
    category TEXT,
    email TEXT,
    website_url TEXT,        -- their current website, if they have one
    maps_url TEXT,          -- their Google Maps listing, to look at on a call
    call_notes TEXT,        -- what they said when you rang
    call_back_at TEXT,      -- ring again after this
    last_called_at TEXT,
    stage TEXT NOT NULL DEFAULT 'found',
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    thread_id TEXT,
    last_rfc_id TEXT,          -- most recent RFC Message-ID in the thread (ours or theirs)
    checkout_generation INTEGER NOT NULL DEFAULT 0,
    stage_before_error TEXT,
    site_html TEXT,
    netlify_site_id TEXT,
    netlify_url TEXT,
    stripe_session_id TEXT,
    stripe_session_url TEXT,
    amount_cents INTEGER,
    email_source TEXT,     -- where JARVIS found the address, when he did
    site_status TEXT,      -- what's wrong with their website: see SITE_* above
    site_note TEXT,        -- the detail behind that verdict
    suggested_email TEXT,
    suggested_email_source TEXT,
    suggested_email_note TEXT,
    researched_at TEXT,
    paid_at TEXT,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_uuid TEXT PRIMARY KEY,
    lead_id INTEGER,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """Thin sqlite wrapper. One connection per thread, WAL mode, atomic claims."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_data_dir(), "solo_studio.db")
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing DBs."""
        c = self._conn()
        have = {row["name"] for row in c.execute("PRAGMA table_info(leads)")}
        for col, decl in (("last_called_at", "TEXT"),
                          ("website_url", "TEXT"),
                          ("maps_url", "TEXT"),
                          ("call_notes", "TEXT"),
                          ("call_back_at", "TEXT"),
                          ("amount_cents", "INTEGER"),
                          ("email_source", "TEXT"),
                          ("site_status", "TEXT"),
                          ("site_note", "TEXT"),
                          ("suggested_email", "TEXT"),
                          ("suggested_email_source", "TEXT"),
                          ("suggested_email_note", "TEXT"),
                          ("researched_at", "TEXT")):
            if col not in have:
                with c:
                    c.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")

    def revenue_cents(self) -> int:
        row = self._conn().execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS r FROM leads"
            " WHERE paid_at IS NOT NULL").fetchone()
        return int(row["r"] or 0)

    def pending_cents(self) -> int:
        """Value of payment links that are out but not yet paid."""
        row = self._conn().execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS r FROM leads"
            " WHERE stage=? AND paid_at IS NULL",
            (STAGE_PAYMENT_LINK_SENT,)).fetchone()
        return int(row["r"] or 0)

    def event_counts(self) -> dict:
        """How many times each event kind has happened, ever."""
        return {row["kind"]: row["n"] for row in self._conn().execute(
            "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")}

    def sends_today(self) -> int:
        """Cold emails sent since midnight UTC (for the daily cap)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='outreach_sent'"
            " AND created_at >= ?", (today,)).fetchone()
        return int(row["n"] or 0)

    def leads_to_call(self) -> list[sqlite3.Row]:
        """Everyone there is to ring, in the order to ring them.

        Call-backs that have come due go first — someone who asked you to try
        again at three o'clock should not be behind ninety strangers. After
        that, whoever you have never tried, then longest-since-tried, so the
        top of the sheet is never someone you rang two minutes ago.
        """
        return self._conn().execute(
            "SELECT * FROM leads WHERE do_not_contact=0"
            " AND phone IS NOT NULL AND phone != ''"
            " AND (stage=? OR (stage=? AND (call_back_at IS NULL"
            "                               OR call_back_at <= ?)))"
            " ORDER BY stage=? DESC,"           # call-backs due first
            "          last_called_at IS NOT NULL ASC,"
            "          last_called_at ASC, id ASC",
            (STAGE_FOUND, STAGE_CALL_BACK, _now(), STAGE_CALL_BACK)).fetchall()

    def distinct_replied_leads(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM events"
            " WHERE kind='reply_received' AND lead_id IS NOT NULL").fetchone()
        return int(row["n"] or 0)

    # -- leads ------------------------------------------------------------

    def add_lead(self, *, place_id, name, address, phone, category, email=None,
                 website_url=None, site_status=None, site_note=None,
                 maps_url=None) -> int | None:
        """Insert a lead; returns new id, or None if this place already exists."""
        c = self._conn()
        try:
            with c:
                cur = c.execute(
                    "INSERT INTO leads (place_id, name, address, phone, category, email,"
                    " website_url, site_status, site_note, maps_url,"
                    " stage, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (place_id, name, address, phone, category, email, website_url,
                     site_status, site_note, maps_url, STAGE_FOUND, _now(), _now()),
                )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def leads_by_stage(self, stage: str) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? ORDER BY updated_at DESC", (stage,)
        ).fetchall()

    def all_leads(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads ORDER BY updated_at DESC").fetchall()

    def find_lead_for_reply(self, thread_id: str | None, from_address: str) -> sqlite3.Row | None:
        """Match an inbound email to a lead: thread id first, then sender address."""
        c = self._conn()
        if thread_id:
            row = c.execute(
                "SELECT * FROM leads WHERE thread_id=?", (thread_id,)).fetchone()
            if row:
                return row
        addr = (from_address or "").strip().lower()
        if not addr:
            return None
        return c.execute(
            "SELECT * FROM leads WHERE lower(email)=? ORDER BY updated_at DESC LIMIT 1",
            (addr,),
        ).fetchone()

    def claim(self, lead_id: int, from_stages: list[str], to_stage: str) -> bool:
        """Atomically move a lead between stages. False means someone else got
        there first (or the lead isn't in an expected stage) — the caller must
        then do nothing. This is the core double-run guard."""
        qmarks = ",".join("?" * len(from_stages))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE leads SET stage=?, updated_at=? WHERE id=? AND stage IN ({qmarks})",
                [to_stage, _now(), lead_id, *from_stages],
            )
        return cur.rowcount == 1

    def update_lead(self, lead_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE leads SET {cols} WHERE id=?",
                      [*fields.values(), lead_id])

    def bump_attempts(self, lead_id: int) -> int:
        with self._conn() as c:
            c.execute("UPDATE leads SET attempts=attempts+1, updated_at=? WHERE id=?",
                      (_now(), lead_id))
        row = self.get_lead(lead_id)
        return row["attempts"] if row else 0

    # -- processed messages ----------------------------------------------

    def message_seen(self, message_uuid: str) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM processed_messages WHERE message_uuid=?",
            (message_uuid,)).fetchone() is not None

    def mark_message_processed(self, message_uuid: str, lead_id: int | None) -> bool:
        """Record a message as handled. False if it was already recorded
        (another poll got it first)."""
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO processed_messages (message_uuid, lead_id, processed_at)"
                    " VALUES (?,?,?)", (message_uuid, lead_id, _now()))
            return True
        except sqlite3.IntegrityError:
            return False

    # -- events / log ------------------------------------------------------

    def log(self, lead_id: int | None, kind: str, detail: str,
            needs_attention: bool = False) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (lead_id, kind, detail, needs_attention, created_at)"
                " VALUES (?,?,?,?,?)",
                (lead_id, kind, detail, 1 if needs_attention else 0, _now()))

    # -- built-in assistant chat -------------------------------------------

    def chat_history(self, limit: int = 40) -> list[sqlite3.Row]:
        """Oldest-first, capped to the most recent `limit` turns."""
        rows = self._conn().execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(rows))

    def chat_add(self, role: str, content: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO chat_messages (role, content, created_at)"
                      " VALUES (?,?,?)", (role, content, _now()))

    def chat_clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM chat_messages")

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def attention_events(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM events WHERE needs_attention=1 AND resolved=0 ORDER BY id DESC"
        ).fetchall()

    def resolve_event(self, event_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE events SET resolved=1 WHERE id=?", (event_id,))

    # -- kv ----------------------------------------------------------------

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def bump_kv(self, key: str, by: int = 1) -> int:
        """Add to a counter and return the new total."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value = CAST(CAST(kv.value AS INTEGER) + ? AS TEXT)",
                (key, str(by), by))
        try:
            return int(self.get_kv(key) or 0)
        except ValueError:
            return 0

    def set_kv(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

WATERMARK_BANNER = (
    '<div id="ss-preview-banner" style="position:fixed;top:0;left:0;right:0;'
    'z-index:2147483647;background:#101418;color:#fff;text-align:center;'
    'padding:10px 16px;font:600 14px/1.4 -apple-system,BlinkMacSystemFont,'
    "'Segoe UI',sans-serif;box-shadow:0 1px 6px rgba(0,0,0,.35)\">"
    'DESIGN PREVIEW — this is a watermarked draft. The final site goes live '
    'once the project is complete.</div>'
)

_WM_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='420' height='300'>"
    "<text x='50%25' y='50%25' text-anchor='middle' fill='rgba(20,20,20,0.10)' "
    "font-family='Helvetica,Arial,sans-serif' font-size='42' font-weight='bold' "
    "transform='rotate(-30 210 150)'>PREVIEW</text></svg>"
)

WATERMARK_OVERLAY = (
    '<div id="ss-preview-overlay" style="position:fixed;inset:0;'
    'z-index:2147483646;pointer-events:none;'
    f'background-image:url(&quot;data:image/svg+xml,{_WM_SVG}&quot;);'
    'background-repeat:repeat"></div>'
)


def inject_watermark(html: str) -> str:
    """Return the watermarked-preview variant of a generated site."""
    marks = WATERMARK_BANNER + WATERMARK_OVERLAY
    m = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if m:
        return html[: m.end()] + marks + html[m.end():]
    return marks + html


# ---------------------------------------------------------------------------
# External services (everything that touches the network lives here so the
# state machine can be tested against fakes)
# ---------------------------------------------------------------------------

class ServiceError(RuntimeError):
    """A service call failed in a way worth showing the user."""


# The signature of an Anthropic account with no API credit, as it lands in the
# event log. One definition, because two places have to agree about it: the
# checkup that raises it, and every button that would otherwise promise work
# Claude is going to refuse.
NO_CREDIT_SIGNS = ("$0 of API credit", "credit balance is too low")


def out_of_credit(db, look_back: int = 40) -> bool:
    """Has a recent call failed for want of Anthropic credit?"""
    try:
        recent = " ".join((e["detail"] or "") for e in db.recent_events(look_back))
    except Exception:
        return False
    return any(sign in recent for sign in NO_CREDIT_SIGNS)


# The failures that actually happen, and what to do about each one. Services
# report these as JSON a paragraph long; what the user needs is one sentence
# and the button to press. Matched against the error text, first hit wins.
PLAIN_ERRORS = (
    ("credit balance is too low",
     "This Anthropic account has $0 of API credit — the key itself is fine. "
     "Add $5 at console.anthropic.com/settings/billing. A Claude Pro or Max "
     "subscription doesn't count, and each account in the console's switcher "
     "has its own balance."),
    ("invalid x-api-key",
     "Claude didn't accept that key. Copy a fresh one from console.anthropic.com "
     "and paste it on the Setup page."),
    ("authentication_error",
     "Claude didn't accept that key. Copy a fresh one from console.anthropic.com "
     "and paste it on the Setup page."),
    ("rate_limit_error",
     "Claude is being asked for too much at once. It sorts itself out — wait a "
     "minute and try again."),
    ("overloaded_error",
     "Claude is overloaded right now. Nothing is broken; try again in a minute."),
    ("api key not valid",
     "Google didn't accept that key. Check it on the Setup page, and make sure "
     "the key has no website or app restriction on it."),
    ("request_denied",
     "Google refused the search. Usually that means the Places API isn't "
     "switched on for this key yet, or billing isn't enabled on the Google "
     "project."),
    ("billing has not been enabled",
     "Google needs billing switched on for the project before it will search. "
     "Google gives $200 of free use a month, so this normally costs nothing."),
    ("service_disabled",
     "The Places API isn't switched on for this Google project yet. Enable "
     "'Places API (New)' in the Google Cloud console."),
    ("invalid api key provided",
     "Stripe didn't accept that key. Copy it again from the Stripe dashboard — "
     "it starts with sk_."),
    ("failed to resolve",
     "No internet connection, or it dropped mid-request. Check the Wi-Fi and "
     "try again."),
    ("max retries exceeded",
     "Couldn't reach the service — probably the internet connection. Try again "
     "in a moment."),
)


def explain(e, limit: int = 200) -> str:
    """Turn a service failure into a sentence the user can act on.

    Anything unrecognised comes through as-is (trimmed), because a raw message
    is still better than a vague one.
    """
    text = str(e).strip()
    low = text.lower()
    for needle, plain in PLAIN_ERRORS:
        if needle in low:
            return plain
    return text[:limit]


# A Facebook page sitting in Google's "website" slot is not a website. It is
# the clearest signal on the whole listing that this business never got one —
# and it is a warmer lead than a blank, because someone there already tried.
# Same for the rest: a profile on somebody else's platform, or a Google
# Business "site" from the builder Google shut down, whose links mostly 404.
SOCIAL_ONLY_HOSTS = (
    "facebook.com", "fb.me", "fb.com", "instagram.com", "linktr.ee",
    "linktree.com", "yelp.com", "business.site", "sites.google.com",
    "g.page", "tiktok.com", "twitter.com", "x.com", "linkedin.com",
    "nextdoor.com", "wa.me", "menulink.online",
)


def social_platform(url: str) -> str:
    """The platform a 'website' link really is, or '' when it's a real site."""
    if not url:
        return ""
    host = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    for known in SOCIAL_ONLY_HOSTS:
        if host == known or host.endswith("." + known):
            return known
    return ""


# What can be wrong with a business's website, worst first. Each one is a
# reason to call them that is true and specific — which is the difference
# between a pitch and spam.
SITE_NONE = "none"            # no website at all: the cleanest pitch there is
SITE_DEAD = "dead"            # the link Google has doesn't load
SITE_PARKED = "parked"        # a placeholder or domain-for-sale page
SITE_SOCIAL = "social"        # a Facebook page standing in for a website
SITE_INSECURE = "insecure"    # http only — browsers label it "Not secure"
SITE_NOT_MOBILE = "not-mobile"  # no viewport: unusable on a phone
SITE_OK = "ok"                # a real, working, modern site — leave them alone

SITE_REASON = {
    SITE_NONE: "no website at all",
    SITE_DEAD: "their website doesn't load",
    SITE_PARKED: "their domain is parked — nothing on it",
    SITE_SOCIAL: "only a social page, no website",
    SITE_INSECURE: "their site is http only, so browsers flag it as not secure",
    SITE_NOT_MOBILE: "their site isn't built for phones",
    SITE_OK: "they already have a working site",
}

# The two statuses that make the strongest pitch: no website at all, or only a
# social page. They are NOT a filter any more — this app rings anyone with a
# phone number, because the call is free and the website situation only decides
# what you open with. See Services._triage. What this tuple still decides is
# which opener you get and how a business is described on the sheet.
LEAD_STATUSES = (SITE_NONE, SITE_SOCIAL)


# What is wrong with their website, said the way you would say it out loud.
# The old app only ever rang businesses with no website at all, so there was
# one opener. Now the sheet has every kind, and the first sentence has to match
# what you are actually looking at while the phone rings.
OPENERS = {
    SITE_NONE: "I noticed you don't have a website up",
    SITE_SOCIAL: "I noticed you've got a Facebook page but no website of your own",
    SITE_DEAD: "I tried to pull up your website and it's not loading",
    SITE_PARKED: "I looked up your website and there's nothing on it — just a "
                 "parked domain",
    SITE_INSECURE: "I pulled up your website and my browser threw a security "
                   "warning at me",
    SITE_NOT_MOBILE: "I pulled your website up on my phone and it's pretty hard "
                     "to use on a small screen",
}
OPENER_DEFAULT = "I had a look at your website"


def call_opener(lead: dict, cfg: dict) -> str:
    """A short thing to say when they answer. Plain, honest, and no API needed.

    Deliberately not generated: it should read the same every time so it can be
    practised, it has to work before any keys are in, and it costs nothing to
    put one in front of every business on the sheet.

    The last line is the whole point of the call. You are not selling on the
    phone — you are asking for an email address so the website can do the
    selling. Everything downstream needs that address and nothing else.
    """
    name = (cfg.get("your_name") or "").strip() or "me"
    studio = (cfg.get("studio_name") or "Solo Studio").strip()
    price = fmt_price(cfg.get("site_price_usd", 500))
    business = lead.get("name") or "your business"
    observed = OPENERS.get(lead.get("site_status") or "", OPENER_DEFAULT)
    offer = ("I can put together a simple one-page site for a flat "
             f"${price} — no monthly fee.")
    if (lead.get("site_status") or SITE_OK) == SITE_OK:
        # Their site actually works. Every other status — down, parked, insecure,
        # unusable on a phone — is a real problem and takes the direct offer;
        # only a working site needs the softer one. Telling someone their site
        # is broken when it isn't gets you caught in the first ten seconds.
        offer = (f"I build simple one-page sites for local businesses, flat "
                 f"${price}, no monthly fee — if yours is due a refresh I'd be "
                 f"happy to show you what a new one could look like.")
    return (
        f"Hi, is this {business}? My name's {name}, I run {studio} — I build "
        f"websites for local businesses.\n\n"
        f"{observed}. {offer}\n\n"
        f"What I'd normally do is design it first so you can see it, and you "
        f"only pay if you like it. What's the best email to send it to?"
    )



# Markers of a page that exists but says nothing. Kept narrow on purpose: a
# false positive here means pitching someone who has a perfectly good site.
PARKED_MARKERS = (
    "this domain is for sale", "domain for sale", "buy this domain",
    "parked free, courtesy", "coming soon", "under construction",
    "site is currently unavailable", "future home of", "godaddy.com/domains",
    "this site can't be reached", "default web page",
)
SITE_TIMEOUT = 8
SITE_WORKERS = 12
SITE_MAX_BYTES = 200_000


EMAIL_RE = re.compile(r"[A-Za-z0-9._%%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Addresses that belong to the web plumbing rather than to the business.
EMAIL_JUNK = ("noreply", "no-reply", "donotreply", "sentry.io", "wixpress",
              "example.com", "@2x", "@sentry", "godaddy", "squarespace",
              "wordpress", "yourdomain", "domain.com", "email.com",
              "sentry-next", "@adobe", "core.js", ".png", ".jpg", ".gif",
              ".webp", ".svg", ".css", "@types")


def scrape_email(url: str, timeout: int = SITE_TIMEOUT) -> tuple[str, str]:
    """Read a contact address straight off a page. Returns (email, url).

    Free — an ordinary web request, no model and no search. Worth trying on
    every lead before anything that costs money: a working-but-dated site
    usually has the address right there in the footer, and so do plenty of
    directory listings.
    """
    if not url:
        return "", ""
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoloStudio/1.0)"})
    except requests.RequestException:
        return "", ""
    try:
        if resp.status_code >= 400:
            return "", ""
        try:
            body = resp.raw.read(SITE_MAX_BYTES, decode_content=True) or b""
        except Exception:
            body = resp.content[:SITE_MAX_BYTES]
        text = body.decode("utf-8", "ignore")
    finally:
        resp.close()

    best = ""
    for found in EMAIL_RE.findall(text):
        low = found.lower()
        if any(bad in low for bad in EMAIL_JUNK) or len(found) > 80:
            continue
        # A generic business address beats a named person's.
        if low.split("@")[0] in ("info", "contact", "hello", "office",
                                 "sales", "admin", "mail"):
            return found, resp.url
        best = best or found
    return best, (resp.url if best else "")


def check_website(url: str, timeout: int = SITE_TIMEOUT) -> tuple[str, str]:
    """Look at a business's website and say what, if anything, is wrong with it.

    Costs nothing — this is an ordinary web request, not a billable API call —
    and it is the difference between "20 of them have websites, sorry" and
    "6 of these websites are broken, here they are".
    """
    if not url:
        return SITE_NONE, ""
    platform = social_platform(url)
    if platform:
        return SITE_SOCIAL, platform
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoloStudio/1.0)"})
    except requests.RequestException as e:
        return SITE_DEAD, type(e).__name__
    try:
        if resp.status_code >= 400:
            return SITE_DEAD, f"HTTP {resp.status_code}"
        try:
            body = resp.raw.read(SITE_MAX_BYTES, decode_content=True) or b""
        except Exception:
            body = resp.content[:SITE_MAX_BYTES]
        text = body.decode("utf-8", "ignore")
        low = text.lower()
        for marker in PARKED_MARKERS:
            if marker in low:
                return SITE_PARKED, marker
        if len(text.strip()) < 500:
            return SITE_PARKED, "almost nothing on the page"
        if not resp.url.lower().startswith("https://"):
            return SITE_INSECURE, resp.url[:120]
        if "name=\"viewport\"" not in low and "name='viewport'" not in low:
            return SITE_NOT_MOBILE, "no viewport tag"
        return SITE_OK, ""
    finally:
        resp.close()


# Things that turn up in a map search and are not small businesses that buy
# websites. A sheriff's office has no website to sell and no owner to sell to.
#
# Two lists, because the two signals are not equally safe. The category comes
# from the map data and can be trusted; a name cannot. "Church Street Auto
# Repair" is a business, and a blunt name test throws it away — so the name
# list holds only phrases that cannot belong to a trading business.
NOT_A_BUSINESS_CATEGORY = (
    "sheriff", "police", "fire department", "fire station", "courthouse",
    "city hall", "town hall", "county clerk", "post office", "dmv",
    "motor vehicles", "library", "school", "university", "college",
    "church", "mosque", "synagogue", "temple", "chapel", "cathedral",
    "place of worship", "hospital", "medical center", "prison", "jail",
    "correctional", "embassy", "consulate", "government", "municipal",
    "township", "national park", "state park", "cemetery", "airport",
    "civic center", "convention center", "city government", "county government",
    "federal", "housing authority", "chamber of commerce", "visitor center",
    "fairground", "courthouse", "public works", "water district",
)
NOT_A_BUSINESS_NAME = (
    # Only phrases that cannot belong to a trading business. "Courthouse
    # Coffee", "Town Hall Tavern" and "The Old Post Office Cafe" are all real
    # businesses, so those words are left to the category test — dropping a
    # genuine lead costs more than letting one town hall through, which the
    # owner can skip in a second.
    "county sheriff", "sheriff s office", "sheriffs office",
    "police department", "police station", "fire department",
    "public library", "high school", "elementary school", "middle school",
    "school district", "board of education", "housing authority",
    "district attorney", "city of", "county of", "town of", "village of",
    "department of", "chamber of commerce",
)


def is_a_business(name: str, category: str = "") -> bool:
    """Is this something a one-person web studio could sell a website to?

    Map searches are full of government offices, schools and churches. They
    are not leads, and putting them in the queue costs the owner the time to
    read past them.
    """
    cat = (category or "").lower()
    if any(word in cat for word in NOT_A_BUSINESS_CATEGORY):
        return False
    low = " " + re.sub(r"\s+", " ",
                       re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())) + " "
    return not any(" " + word + " " in low for word in NOT_A_BUSINESS_NAME)


def _place_row(p: dict) -> dict:
    """One Google place, in the shape a lead is stored in."""
    kind = p.get("primaryTypeDisplayName")
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", "Unknown"),
        "address": p.get("formattedAddress"),
        "phone": p.get("nationalPhoneNumber"),
        "category": kind.get("text") if isinstance(kind, dict) else kind,
        "website_url": p.get("websiteUri") or None,
        "maps_url": p.get("googleMapsUri") or None,
    }


def _town_of(address: str) -> str:
    """The town out of a postal address, for when no home town was set.

    "12 Main St, Ellenville, NY 12428" -> "Ellenville, NY". Rough on purpose:
    it only has to be good enough to look up on a map, and anything it gets
    wrong the owner can correct on Setup.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return ""
    town = parts[-2]
    state = parts[-1].split()[0] if parts[-1].split() else ""
    if len(state) == 2 and state.isalpha():
        return f"{town}, {state.upper()}"
    return town


def _domain_of(url: str) -> str:
    """The bare domain of a real website, or "" for a social page or nothing.

    A Facebook page has no domain of the business's own, so there is nothing
    for a domain-search to look up.
    """
    if not url or social_platform(url):
        return ""
    host = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _osm_row(el: dict) -> dict | None:
    """One OpenStreetMap element, in the shape a lead is stored in.

    Skips anything without a name — an unnamed point on a map is not a
    business anyone can be written to.
    """
    tags = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    street = " ".join(x for x in (tags.get("addr:housenumber"),
                                  tags.get("addr:street")) if x)
    town = " ".join(x for x in (tags.get("addr:city"), tags.get("addr:state"),
                                tags.get("addr:postcode")) if x)
    address = ", ".join(x for x in (street, town) if x)
    trade = (tags.get("shop") or tags.get("craft") or tags.get("office")
             or tags.get("amenity") or "")
    # OpenStreetMap sometimes just has the email in it. That is the cheapest
    # address there is: no lookup, no model, no search.
    email = (tags.get("email") or tags.get("contact:email") or "").strip()
    return {
        "place_id": "osm:%s/%s" % (el.get("type"), el.get("id")),
        "name": name,
        "address": address or None,
        "phone": (tags.get("phone") or tags.get("contact:phone")
                  or tags.get("contact:mobile") or None),
        "category": trade.replace("_", " ").title() or None,
        "email": email if EMAIL_RE.fullmatch(email) else None,
        "email_source": ("https://www.openstreetmap.org/%s/%s"
                         % (el.get("type"), el.get("id"))) if email else None,
        "website_url": (tags.get("website") or tags.get("contact:website")
                       or tags.get("contact:facebook") or None),
        "maps_url": "https://www.openstreetmap.org/%s/%s" % (el.get("type"),
                                                            el.get("id")),
    }


class SearchResults(list):
    """Leads found, plus what was passed over on the way there.

    A search that comes back empty is not the same as a search that went
    wrong, and neither is the same as a search whose every result already sits
    in the database. Carrying the counts means the app can say which.
    """

    def __init__(self, *a):
        super().__init__(*a)
        self.seen = 0            # businesses the source returned
        self.with_site = 0       # kept, but they already have a good website
        self.closed = 0          # rejected: permanently closed
        self.social_only = 0     # kept: only a Facebook/Instagram/Yelp page
        self.by_status = {}      # kept, counted by what's wrong with their site
        self.not_business = 0    # rejected: a school, a church, a sheriff
        self.no_phone = 0        # rejected: nothing to ring


def describe_search(r: dict) -> str:
    """One sentence saying what a search actually did.

    A search that finds nothing used to say nothing at all, which left the
    only honest question — why? — with no answer anywhere in the app. Every
    outcome here names its own reason.
    """
    seen = r.get("seen", 0)
    if not seen:
        return ("Google returned no businesses at all — check the spelling of "
                "the town, or try a bigger one nearby.")
    added, found = r.get("added", 0), r.get("found", 0)
    bits = [f"looked at {seen}"]
    if r.get("with_site"):
        bits.append(f"{r['with_site']} have a working site")
    if r.get("closed"):
        bits.append(f"{r['closed']} closed down")
    by = r.get("by_status") or {}
    if by:
        worth = ", ".join(
            "%d %s" % (n, SITE_REASON.get(k, k))
            for k, n in sorted(by.items(), key=lambda kv: -kv[1]))
        bits.append("worth pitching: " + worth)
    elif found:
        bits.append(f"{found} worth pitching")
    head = ", ".join(bits)
    if added:
        return f"{head} — {added} new."
    if found:
        return f"{head} — all already in your list."
    return (f"{head}. Every site here loads and works; smaller towns nearby "
            "are where the gaps are.")


# ---------------------------------------------------------------------------
# The watchman
#
# One deterministic pass over the config, the leads and the log that answers
# the only question that matters when you open the app: is anything wrong, and
# what do I do about it. No API calls, no model, no guessing — it runs on every
# background tick and on every page load, so it has to be cheap and it has to
# be the same answer every time.
#
# It reports. It never acts: nothing here sends an email, moves money, or
# touches a lead's stage.
# ---------------------------------------------------------------------------

# The five keys, by the name the owner sees on Setup.
API_KEYS = (("Anthropic (Claude)", "anthropic_api_key"),
            ("Inkbox", "inkbox_api_key"),
            ("Netlify", "netlify_api_key"),
            ("Stripe", "stripe_secret_key"),
            ("Google Places", "google_places_api_key"))

FIX = "fix"            # broken — the pipeline can't do its job until it's sorted
WAITING = "waiting"    # working, but it needs a human decision
WATCH = "watch"        # worth knowing, nothing on fire
LEVEL_ORDER = {FIX: 0, WAITING: 1, WATCH: 2}

# How long a lead may sit in a stage that is supposed to take seconds before
# it counts as stuck rather than busy. Time alone is not enough: a step that
# keeps failing and retrying refreshes the lead every time, so it never looks
# old. Repeated attempts catch that one.
STUCK_MINUTES = 30
STUCK_ATTEMPTS = 2
# A payment link nobody has used, and a preview nobody answered, go stale.
STALE_LINK_DAYS = 7
STALE_PREVIEW_DAYS = 5


def _age_minutes(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def finding(id, level, title, detail, where="/", cta="") -> dict:
    return {"id": id, "level": level, "title": title, "detail": detail,
            "where": where, "cta": cta}


def checkup(db, cfg, key_fields=API_KEYS, extra=(),
            spent: int = None, cap: int = None) -> list[dict]:
    """Everything currently worth telling the owner, worst first."""
    out = []
    cap = cap if cap is not None else setting_int(
        cfg, "monthly_google_cap", GOOGLE_CALL_CAP)

    # -- can it work at all -------------------------------------------------
    missing = [name for name, field in key_fields if not cfg.get(field)]
    if missing:
        out.append(finding(
            "keys-missing", FIX,
            "%d API key%s still missing" % (len(missing),
                                            "" if len(missing) == 1 else "s"),
            "Nothing runs without " + ", ".join(missing) + ".",
            "/setup", "Open Setup"))

    if not cfg.get("mailing_address"):
        out.append(finding(
            "no-mailing-address", FIX, "No mailing address saved",
            "Cold email has to carry a real postal address by law. Add yours "
            "before any outreach goes out.", "/setup", "Add it"))
    if not cfg.get("your_name"):
        out.append(finding(
            "no-name", WAITING, "Your name isn't filled in",
            "Every email signs off with it.", "/setup", "Add it"))

    if cfg.get("phone_access_enabled") and not cfg.get("phone_pin"):
        out.append(finding(
            "phone-open", FIX, "Phone access is on with no PIN",
            "Anyone on your Wi-Fi can open your dashboard. Set a PIN or turn "
            "phone access off.", "/setup", "Fix it"))

    # -- what the log is complaining about ----------------------------------
    attention = db.attention_events()
    if attention:
        kinds = {}
        for e in attention:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        worst = ", ".join(f"{k.replace('_', ' ')} ({v})"
                          for k, v in sorted(kinds.items(),
                                             key=lambda kv: -kv[1])[:3])
        out.append(finding(
            "attention", FIX,
            "%d thing%s went wrong" % (len(attention),
                                       "" if len(attention) == 1 else "s"),
            worst + ". Each one is on the Activity page with what happened.",
            "/activity", "See what"))

    if out_of_credit(db):
        out.append(finding(
            "credit-empty", FIX, "Claude has no API credit",
            "Every step that writes or designs anything is failing. Add credit "
            "at console.anthropic.com/settings/billing.", "/setup", "Open Setup"))

    # -- leads that are stuck or broken -------------------------------------
    leads = db.all_leads()
    errored = [l for l in leads if l["stage"] == STAGE_ERROR]
    if errored:
        out.append(finding(
            "leads-error", FIX,
            "%d lead%s gave up with an error" % (len(errored),
                                                 "" if len(errored) == 1 else "s"),
            ", ".join(l["name"] for l in errored[:3])
            + ("..." if len(errored) > 3 else "")
            + ". Their last error is on the lead.", "/", "Look"))

    stuck, retrying = [], []
    for l in leads:
        if l["stage"] not in TRANSIENT_STAGES:
            continue
        if (l["attempts"] or 0) >= STUCK_ATTEMPTS:
            retrying.append(l)
        elif _age_minutes(l["updated_at"]) > STUCK_MINUTES:
            stuck.append(l)
    if stuck or retrying:
        n = len(stuck) + len(retrying)
        why = ("failing and retrying" if retrying else
               "sitting there for over %d minutes" % STUCK_MINUTES)
        out.append(finding(
            "leads-stuck", FIX,
            "%d lead%s stuck mid-step" % (n, "" if n == 1 else "s"),
            "These stages take seconds. %s means something is going wrong "
            "quietly — Activity says what."
            % why.capitalize(), "/activity", "See why"))

    # -- waiting on the human ----------------------------------------------
    # There is only one thing this app ever waits on you for, and it is the
    # one thing software cannot do: pick up the phone.
    to_call = db.leads_to_call()
    if to_call:
        out.append(finding(
            "call-sheet", WAITING,
            "%d business%s ready to call" % (len(to_call),
                                             "" if len(to_call) == 1 else "es"),
            "Every one has a phone number and a look at their current website. "
            "Nothing moves until one of them says yes.", "/", "Open the sheet"))
    elif not db.all_leads():
        out.append(finding(
            "no-leads", FIX, "Nothing to call yet",
            "Set your area on Setup and the call sheet fills itself.",
            "/setup", "Set your area"))

    # -- quietly going cold -------------------------------------------------
    stale_links = [l for l in leads if l["stage"] == STAGE_PAYMENT_LINK_SENT
                   and _age_minutes(l["updated_at"]) > STALE_LINK_DAYS * 1440]
    if stale_links:
        out.append(finding(
            "payment-stale", WATCH,
            "%d payment link older than %d days" % (len(stale_links),
                                                    STALE_LINK_DAYS),
            "Sent and never used. Worth a phone call.", "/calls", "Call"))

    stale_previews = [l for l in leads if l["stage"] == STAGE_PREVIEW_SENT
                      and _age_minutes(l["updated_at"]) > STALE_PREVIEW_DAYS * 1440]
    if stale_previews:
        out.append(finding(
            "preview-stale", WATCH,
            "%d preview with no answer in %d days" % (len(stale_previews),
                                                      STALE_PREVIEW_DAYS),
            "They saw a site with their name on it and went quiet.",
            "/calls", "Call"))

    # -- reasons JARVIS might be doing nothing ------------------------------
    # Silence is the worst outcome here. If he isn't working, that is the most
    # important thing on the screen, not something to leave the owner guessing
    # about.
    if not cfg.get("autopilot_enabled"):
        out.append(finding(
            "autopilot-off", FIX, "JARVIS is switched off",
            "He isn't hunting for leads, reading replies or checking payments. "
            "Nothing happens on its own until this is back on.",
            "/setup", "Turn him on"))
    elif not cfg.get("auto_search_enabled"):
        out.append(finding(
            "search-off", FIX, "Lead hunting is switched off",
            "JARVIS is running, but he won't go looking for anyone. No new "
            "leads will appear on their own.", "/setup", "Turn it on"))
    elif spent is not None and spent >= cap:
        out.append(finding(
            "google-spent", FIX, "This month's search budget is used up",
            "%d Google searches used of the %d you allow, so hunting has "
            "stopped until the 1st. Raise the cap in Setup if you want more "
            "(the first 5,000 a month are free)." % (spent, cap),
            "/setup", "Setup"))

    if str(cfg.get("stripe_secret_key", "")).startswith("sk_test"):
        out.append(finding(
            "stripe-test", WATCH, "Stripe is in test mode",
            "Perfect for a practice run — but no real money can be taken.",
            "/setup", "Setup"))

    out.extend(extra)
    out.sort(key=lambda f: LEVEL_ORDER.get(f["level"], 9))
    return out


class Services:
    def __init__(self, config: dict, meter=None):
        self.config = config
        self._inkbox = None
        self._identity = None
        self._anthropic = None
        # Called once per billable Google call. It may refuse, which is the
        # only thing standing between "JARVIS does everything" and a bill.
        self.meter = meter

    # -- Google Places (New) ----------------------------------------------

    def places_search(self, query: str,
                                 max_results: int = 60) -> SearchResults:
        """Text-search businesses and keep the ones with no website of their own.

        "No website" includes a listing whose only link is a Facebook page or
        the like: that business has nowhere of its own to send a customer,
        which is the entire pitch. Counts of what was passed over come back
        with the results so an empty search can explain itself.
        """
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        url = "https://places.googleapis.com/v1/places:searchText"
        field_mask = ",".join([
            "places.id", "places.displayName", "places.formattedAddress",
            "places.nationalPhoneNumber", "places.websiteUri",
            "places.primaryTypeDisplayName", "places.businessStatus",
            "places.googleMapsUri",
            "nextPageToken",
        ])
        headers = {
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        }
        results = SearchResults()
        raw: list[dict] = []
        page_token = None
        while len(results) < max_results:
            body: dict = {"textQuery": query, "pageSize": 20}
            if page_token:
                body["pageToken"] = page_token
            if self.meter:
                self.meter()
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code != 200:
                raise ServiceError(
                    f"Google Places error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            for p in data.get("places", []):
                results.seen += 1
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    results.closed += 1
                    continue
                raw.append({
                    "place_id": p.get("id"),
                    "name": (p.get("displayName") or {}).get("text", "Unknown"),
                    "address": p.get("formattedAddress"),
                    "phone": p.get("nationalPhoneNumber"),
                    "category": p.get("primaryTypeDisplayName", {}).get("text")
                    if isinstance(p.get("primaryTypeDisplayName"), dict)
                    else p.get("primaryTypeDisplayName"),
                    "website_url": p.get("websiteUri") or None,
        "maps_url": p.get("googleMapsUri") or None,
                })
            page_token = data.get("nextPageToken")
            if not page_token or len(raw) >= max_results:
                break

        return self._triage(raw, results, max_results)

    def _triage(self, raw: list[dict], results: "SearchResults",
                max_results: int) -> "SearchResults":
        """Keep the businesses you can actually ring.

        This is the one rule that makes this app different from the last one.
        The old rule was "keep them only if they have no website", which is the
        better pitch and turned out to be unworkable: a business with no website
        has no email anywhere either, so there was no way to reach them.

        The rule now is **a phone number**. You are going to ring them, and a
        business you cannot ring is no use however good the pitch would have
        been. Their website is still looked at, but it is context for the call
        rather than a filter — whether you open with "you haven't got one",
        "yours is down", or "yours doesn't work on a phone" is something you
        decide while the phone rings.

        Every source comes through here, so a lead means the same thing
        whichever index it was found in.
        """
        wanted, dropped = [], 0
        for lead in raw:
            if is_a_business(lead.get("name"), lead.get("category")):
                wanted.append(lead)
            else:
                dropped += 1
        raw = wanted
        results.not_business = dropped
        for lead, (status, note) in zip(raw, self._check_sites(raw)):
            lead["site_status"] = status
            lead["site_note"] = note[:200] if note else None
            if not (lead.get("phone") or "").strip():
                results.no_phone += 1
                continue
            if status not in LEAD_STATUSES:
                results.with_site += 1      # counted, but still worth calling
            if status == SITE_SOCIAL:
                results.social_only += 1
            results.by_status[status] = results.by_status.get(status, 0) + 1
            results.append(lead)
        del results[max_results:]
        return results

    # -- Google, by distance rather than fame --------------------------------

    def places_geocode(self, area: str) -> tuple[float, float] | None:
        """Where a town is. One search, and the answer never changes."""
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        if self.meter:
            self.meter()
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={"X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.location",
                     "Content-Type": "application/json"},
            json={"textQuery": area, "pageSize": 1}, timeout=30)
        if resp.status_code != 200:
            raise ServiceError(
                f"Google Places error {resp.status_code}: {resp.text[:300]}")
        places = resp.json().get("places") or []
        if not places:
            return None
        loc = places[0].get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            return None
        return float(loc["latitude"]), float(loc["longitude"])

    def places_nearby(self, lat: float, lng: float, radius: int = 4000,
                      types: list[str] = None,
                      max_results: int = 20) -> SearchResults:
        """The businesses *nearest* a point, rather than the best known ones.

        This is the difference that matters. A text search ranks by prominence,
        which is almost a definition of "has a website"; ranking by distance
        returns the one-van operation on the side street, which is the whole
        market. Twenty per call, no pagination — so sweep with several points
        rather than asking for more.
        """
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        body = {
            "maxResultCount": max(1, min(20, max_results)),
            "rankPreference": "DISTANCE",
            "locationRestriction": {"circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(max(1, min(50000, radius)))}},
        }
        if types:
            body["includedTypes"] = list(types)
        raw = self._nearby_call(body)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    def _nearby_call(self, body: dict) -> list[dict]:
        """One Nearby Search. Retries without the type filter if Google
        rejects a type name — a wrong type would silently return nothing,
        which is worse than a broader search."""
        key = self.config.get("google_places_api_key", "")
        headers = {
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": ",".join([
                "places.id", "places.displayName", "places.formattedAddress",
                "places.nationalPhoneNumber", "places.websiteUri",
                "places.primaryTypeDisplayName", "places.businessStatus",
                "places.googleMapsUri"]),
            "Content-Type": "application/json",
        }
        for attempt in (body, {k: v for k, v in body.items()
                               if k != "includedTypes"}):
            if self.meter:
                self.meter()
            resp = requests.post(
                "https://places.googleapis.com/v1/places:searchNearby",
                headers=headers, json=attempt, timeout=30)
            if resp.status_code == 200:
                return [_place_row(p) for p in resp.json().get("places", [])
                        if p.get("businessStatus") in (None, "OPERATIONAL")]
            if resp.status_code != 400 or "includedTypes" not in attempt:
                raise ServiceError(
                    f"Google Places error {resp.status_code}: {resp.text[:300]}")
        return []

    def osm_nearby(self, lat: float, lng: float, radius: int = 5000,
                   max_results: int = 60) -> SearchResults:
        """Local businesses from OpenStreetMap.

        A completely separate index from Google's, free, no key, and full of
        exactly the small operators this app is looking for — an OSM entry
        very often has a name and a phone number and no website at all.
        """
        parts = "".join(f"  {sel}(around:{int(radius)},{lat},{lng});\n"
                        for sel in self.OSM_SELECTORS)
        query = (f"[out:json][timeout:40];\n(\n{parts});\n"
                 f"out center {self.OVERPASS_MAX};")
        last = "no mirror answered"
        elements = None
        for url in self.OVERPASS_URLS:
            try:
                resp = requests.post(
                    url, data={"data": query}, timeout=60,
                    headers={"User-Agent":
                             "SoloStudio/1.0 (local business finder)"})
            except requests.RequestException as e:
                last = str(e)
                continue
            if resp.status_code in (429, 502, 503, 504):
                last = "busy (HTTP %d)" % resp.status_code
                continue        # a free service is allowed to be busy
            if resp.status_code != 200:
                raise ServiceError(
                    f"OpenStreetMap error {resp.status_code}: {resp.text[:200]}")
            try:
                elements = resp.json().get("elements", [])
            except ValueError as e:
                raise ServiceError(
                    f"OpenStreetMap sent something odd: {e}") from e
            break
        if elements is None:
            raise ServiceError(
                "Couldn't reach OpenStreetMap — it's free and run by "
                "volunteers, so it's sometimes busy. Google searching is "
                "unaffected. (%s)" % last[:120])

        raw = []
        for el in elements:
            row = _osm_row(el)
            if row:
                raw.append(row)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    def _check_sites(self, leads: list[dict]) -> list[tuple[str, str]]:
        """Verdicts for a batch of websites, looked at side by side.

        One at a time this would be minutes. A business with no link at all
        costs nothing and never leaves the process.
        """
        from concurrent.futures import ThreadPoolExecutor
        urls = [(lead.get("website_url") or "") for lead in leads]
        if not any(urls):
            return [(SITE_NONE, "")] * len(urls)
        with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
            return list(pool.map(check_website, urls))

    # -- Inkbox email ------------------------------------------------------

    def _get_identity(self):
        if self._identity is not None:
            return self._identity
        try:
            from inkbox import Inkbox
        except ImportError as e:
            raise ServiceError("The 'inkbox' package is not installed.") from e
        key = self.config.get("inkbox_api_key", "")
        if not key:
            raise ServiceError("Inkbox API key is not set (see Setup).")
        self._inkbox = Inkbox(api_key=key)
        handle = (self.config.get("inkbox_agent_handle") or "").strip()
        if handle:
            self._identity = self._inkbox.get_identity(handle)
        else:
            identities = self._inkbox.list_identities()
            if len(identities) == 1:
                self._identity = self._inkbox.get_identity(identities[0].agent_handle)
            elif not identities:
                raise ServiceError("No Inkbox identities exist on this account.")
            else:
                names = ", ".join(i.agent_handle for i in identities)
                raise ServiceError(
                    f"Multiple Inkbox identities found ({names}); pick one in Setup.")
        if not self._identity.email_address:
            raise ServiceError(
                "The selected Inkbox identity has no email mailbox assigned.")
        return self._identity

    def email_send(self, *, to: str, subject: str, body_text: str,
                   in_reply_to_rfc_id: str | None = None) -> dict:
        """Send an email; returns {thread_id, rfc_id, message_uuid}."""
        identity = self._get_identity()
        msg = identity.send_email(
            to=[to], subject=subject, body_text=body_text,
            in_reply_to_message_id=in_reply_to_rfc_id,
        )
        return {
            "thread_id": str(msg.thread_id) if msg.thread_id else None,
            "rfc_id": msg.message_id,
            "message_uuid": str(msg.id),
        }

    def email_inbound_since(self, since_iso: str | None) -> list[dict]:
        """All inbound emails since a timestamp (oldest first)."""
        from inkbox import MessageDirection
        identity = self._get_identity()
        out = []
        for msg in identity.iter_emails(direction=MessageDirection.INBOUND,
                                        start_datetime=since_iso):
            out.append({
                "message_uuid": str(msg.id),
                "thread_id": str(msg.thread_id) if msg.thread_id else None,
                "rfc_id": msg.message_id,
                "from_address": msg.from_address,
                "subject": msg.subject or "",
                "snippet": msg.snippet or "",
                "created_at": msg.created_at.isoformat(),
            })
        out.reverse()  # API yields newest first; process oldest first
        return out

    def email_fetch_body(self, message_uuid: str) -> str:
        """Full body text of one message. NOTE: for inbound mail Inkbox marks
        the message read server-side when fetched — we keep our own processed
        table and never rely on the unread flag."""
        identity = self._get_identity()
        detail = identity.get_message(message_uuid)
        if detail.body_text:
            return detail.body_text
        if detail.body_html:
            return re.sub(r"<[^>]+>", " ", detail.body_html)
        return ""

    # -- Claude ------------------------------------------------------------

    def _get_anthropic(self):
        if self._anthropic is not None:
            return self._anthropic
        try:
            import anthropic
        except ImportError as e:
            raise ServiceError("The 'anthropic' package is not installed.") from e
        key = self.config.get("anthropic_api_key", "")
        if not key:
            raise ServiceError("Anthropic API key is not set (see Setup).")
        self._anthropic = anthropic.Anthropic(api_key=key)
        return self._anthropic

    def generate_site_html(self, lead: dict) -> str:
        """Ask Claude to design the site. Returns clean (no watermark) HTML."""
        client = self._get_anthropic()
        model = self.config.get("anthropic_model") or "claude-opus-5"
        details = [f"Business name: {lead['name']}"]
        if lead.get("category"):
            details.append(f"Type of business: {lead['category']}")
        if lead.get("address"):
            details.append(f"Address: {lead['address']}")
        if lead.get("phone"):
            details.append(f"Phone: {lead['phone']}")
        # Deliberately the best model available, whatever the spending dial
        # says: this only runs once somebody has asked for a site, it is the
        # thing being sold, and it is the one place quality is worth paying for.
        prompt = (
            "Design a beautiful, modern, single-page website for this local business:\n\n"
            + "\n".join(details) + "\n\n"
            "Requirements:\n"
            "- One complete, self-contained HTML file (all CSS and JS inline).\n"
            "- Professional and tasteful; pick a palette and typography that fit the "
            "business type. Mobile-responsive.\n"
            "- Sections: hero, about/services, and a contact section showing the real "
            "phone number and address above.\n"
            "- Do NOT invent facts (no fake reviews, prices, hours, or team members). "
            "Where such content would normally go, use graceful generic copy.\n"
            "- No external images; use CSS/inline SVG for visuals.\n"
            "- Output ONLY the HTML document, no commentary."
        )
        with client.messages.stream(
            model=model, max_tokens=48000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.explanation})"
            raise ServiceError("Claude declined to generate this site" + detail)
        text = "".join(b.text for b in response.content if b.type == "text")
        html = _extract_html(text)
        if not html:
            raise ServiceError("Claude's response did not contain an HTML document.")
        return html

    def classify_reply(self, lead: dict, stage: str, body: str) -> str:
        """Classify an inbound reply. Returns one of:
        interested | declined | unsubscribe | unclear.

        The prompt is deliberately conservative — anything ambiguous comes back
        as "unclear" for a person to read — which is what makes the small model
        safe here when the dial asks for it.
        """
        client = self._get_anthropic()
        model = thinking_model(self.config)
        context = {
            STAGE_CONTACTED: "We cold-emailed them offering to design a free website preview.",
            STAGE_PREVIEW_SENT: "We sent them a link to a free preview of their website.",
            STAGE_PAYMENT_LINK_SENT: "We sent them a payment link for the website.",
        }.get(stage, "We are corresponding with them about a website.")
        prompt = (
            f"You triage replies for a small web design studio. {context}\n"
            f"The business ({lead['name']}) replied with the email below.\n\n"
            "Classify the reply. Answer with EXACTLY one word:\n"
            "- interested  (they want to proceed / like it / say yes)\n"
            "- declined    (not interested, no thanks)\n"
            "- unsubscribe (stop contacting me, remove me, spam complaint)\n"
            "- unclear     (questions, requests for changes, anything else)\n\n"
            "When in doubt choose unclear — a human will handle it. Never choose "
            "interested unless the reply clearly says to go ahead.\n\n"
            f"--- REPLY ---\n{body[:4000]}\n--- END ---"
        )
        response = client.messages.create(
            model=model, max_tokens=200,   # the answer is one word
            **({} if model.startswith("claude-haiku")
               else {"output_config": {"effort": "low"}}),
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return "unclear"
        text = "".join(b.text for b in response.content if b.type == "text").lower()
        for intent in ("unsubscribe", "interested", "declined", "unclear"):
            if intent in text:
                return intent
        return "unclear"

    ASSISTANT_BRIEF = """You are the built-in helper inside Solo Studio, a Mac \
app that runs a one-person web design business end to end. You are talking to \
its owner, who is not a programmer. Be warm, brief and concrete.

HOW SOLO STUDIO WORKS
Leads move through fixed stages, in order:
  found -> contacted -> building_preview -> preview_sent ->
  sending_payment_link -> payment_link_sent -> paid -> deploying_final -> delivered
Plus not_interested (they said no) and error (something failed; it can be retried).

1. Scout finds local businesses with no website (Google Places).
2. Researcher hunts each one's public email with web search. It only SUGGESTS;
   the owner accepts or rejects every address.
3. Copywriter drafts the cold email. Nothing is ever sent until the owner taps
   "Approve & send" on the Approve page. There is no auto-send.
4. Triage reads replies and judges interest. Anything ambiguous is parked in
   "Needs your attention" rather than guessed at.
5. Designer builds a one-page site; Deployer publishes a WATERMARKED preview to
   Netlify and emails the link.
6. On a second positive reply, Biller emails a Stripe payment link.
7. Delivery ships the clean, watermark-free site ONLY after Stripe confirms the
   payment cleared, and emails the live link.

THE PAYMENT GATE — never suggest working around this. The final site cannot be
delivered unless Stripe itself reports the payment as paid. It is re-checked at
delivery time, and there is no button or setting that skips it. If the owner
asks to send a site before payment, tell them plainly that the app will not do
that, and suggest sending another preview instead.

WHERE THINGS ARE IN THE APP
- Dashboard — every lead and its stage.
- Approve — found businesses waiting for the owner's OK, showing the exact email
  word for word. Also where a missing email address gets pasted in.
- Team — the eight specialists and what each has done.
- Activity — the full log.
- Setup — API keys (with click-by-click directions), business details, the cold
  email template, automatic lead hunting, phone access, and an Advanced section.
- Updates — install a new version, then restart.
- JARVIS — the live stats screen, with the same watch list on it.

THE WATCH LIST
The app checks itself continuously and the snapshot below opens with what it
found, worst first: FIX means something is broken, WAITING means it needs a
decision only the owner can make, WATCH means worth knowing. That list is on
their screen too, so it is the shared starting point. When they ask what is
wrong, or what to do next, answer from it — name the top item, say what it
means in their words, and where to go. Do not invent problems that are not on
it, and do not soften one that is.

WHAT YOU CAN AND CANNOT DO
You can see the owner's live pipeline (below) and answer anything about it, walk
them through setup, explain why a lead is stuck, suggest wording, and do the
arithmetic on their numbers.
You CANNOT act. You cannot send email, approve a lead, create a payment link,
deploy a site, move money, or change any setting — you have no ability to do any
of it. So never say you have done something or will do it later. Instead name
the page and the button: "open Approve and tap Approve & send on Rivera
Plumbing". If something needs a decision only they can make, say so.

STYLE
Short paragraphs, plain words, no jargon or code unless they ask. Two or three
sentences is usually enough. Use their real numbers from the snapshot rather
than speaking generally. If the snapshot does not contain the answer, say what
you do not know instead of inventing a lead, a figure, or a setting."""

    def assistant_reply(self, history: list[dict], snapshot: str) -> str:
        """Answer the owner's question about their own pipeline.

        Advisory only: no tools are wired up, so this cannot act on anything.
        """
        client = self._get_anthropic()
        # Stays on the best model whatever the dial says: you ask this a
        # handful of times a month, deliberately, and a worse answer to "what
        # should I do next" is not worth the fraction of a penny saved.
        model = (self.config.get("anthropic_model") or "").strip() or MAIN_MODEL
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if m.get("content")]
        if not messages:
            raise ServiceError("Nothing to answer.")
        with client.messages.stream(
            model=model,
            max_tokens=2000,
            **({} if model.startswith("claude-haiku")
               else {"output_config": {"effort": "low"}}),
            system=[{"type": "text",
                     "text": self.ASSISTANT_BRIEF,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": snapshot}],
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return ("I wasn't able to answer that one. Try rephrasing it, or ask "
                    "me something about your leads or setup.")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or "I didn't have anything to add there — try asking again."

    def push_notify(self, title: str, message: str, priority: str = "default",
                    tags: str = "") -> None:
        """Send a push notification to the user's phone via ntfy.sh.
        The topic name acts as the secret — nothing sensitive is included
        beyond lead name + event. Raises ServiceError on failure so the
        Setup test button can report it; pipeline callers swallow errors."""
        if not self.config.get("ntfy_enabled"):
            return
        topic = (self.config.get("ntfy_topic") or "").strip()
        if not topic:
            raise ServiceError("Notifications are enabled but no topic is set.")
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        resp = requests.post(f"https://ntfy.sh/{topic}",
                             data=message.encode("utf-8"),
                             headers=headers, timeout=10)
        if resp.status_code != 200:
            raise ServiceError(f"ntfy.sh error {resp.status_code}: {resp.text[:200]}")

    # -- Netlify -----------------------------------------------------------

    def _netlify_headers(self) -> dict:
        key = self.config.get("netlify_api_key", "")
        if not key:
            raise ServiceError("Netlify API key is not set (see Setup).")
        return {"Authorization": f"Bearer {key}"}

    def netlify_deploy(self, site_id: str | None, html: str,
                       extra_files: dict[str, str] | None = None) -> dict:
        """Deploy a one-page site via zip upload. Creates the site if needed.
        Returns {site_id, url}."""
        headers = self._netlify_headers()
        if site_id is None:
            resp = requests.post("https://api.netlify.com/api/v1/sites",
                                 headers=headers, json={}, timeout=30)
            if resp.status_code not in (200, 201):
                raise ServiceError(
                    f"Netlify site creation failed {resp.status_code}: {resp.text[:300]}")
            site = resp.json()
            site_id = site["id"]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", html)
            for name, content in (extra_files or {}).items():
                zf.writestr(name, content)
        buf.seek(0)
        resp = requests.post(
            f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
            headers={**headers, "Content-Type": "application/zip"},
            data=buf.getvalue(), timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise ServiceError(
                f"Netlify deploy failed {resp.status_code}: {resp.text[:300]}")
        deploy = resp.json()
        deploy_id = deploy["id"]
        # Wait for the deploy to finish processing.
        url = deploy.get("ssl_url") or deploy.get("url")
        for _ in range(30):
            r = requests.get(f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
                             headers=headers, timeout=30)
            if r.status_code == 200:
                d = r.json()
                url = d.get("ssl_url") or d.get("url") or url
                if d.get("state") == "ready":
                    break
                if d.get("state") == "error":
                    raise ServiceError("Netlify deploy ended in error state.")
            time.sleep(2)
        if not url:
            raise ServiceError("Netlify deploy finished but returned no URL.")
        return {"site_id": site_id, "url": url.rstrip("/")}

    # -- Stripe ------------------------------------------------------------

    def _stripe_auth(self) -> dict:
        key = self.config.get("stripe_secret_key", "")
        if not key:
            raise ServiceError("Stripe secret key is not set (see Setup).")
        return {"Authorization": f"Bearer {key}"}

    def stripe_create_checkout(self, lead: dict, preview_url: str) -> dict:
        """Create a Checkout Session for the flat site price.
        Returns {session_id, url}."""
        amount_cents = int(round(float(self.config.get("site_price_usd", 500)) * 100))
        currency = self.config.get("currency", "usd")
        data = {
            "mode": "payment",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][product_data][name]":
                f"Website for {lead['name']}",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][quantity]": "1",
            "success_url": f"{preview_url}/thanks.html",
            "cancel_url": preview_url,
            "metadata[lead_id]": str(lead["id"]),
        }
        if lead.get("email"):
            data["customer_email"] = lead["email"]
        headers = {
            **self._stripe_auth(),
            # One session per (lead, checkout generation): retries after a network
            # error map to the same session instead of creating a second link.
            "Idempotency-Key":
                f"solo-studio-lead-{lead['id']}-gen-{lead.get('checkout_generation', 0)}",
        }
        resp = requests.post("https://api.stripe.com/v1/checkout/sessions",
                             headers=headers, data=data, timeout=30)
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ServiceError(f"Stripe checkout creation failed: {msg}")
        session = resp.json()
        return {"session_id": session["id"], "url": session["url"]}

    def stripe_get_session(self, session_id: str) -> dict:
        """Fetch a Checkout Session's live status straight from Stripe."""
        resp = requests.get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            headers=self._stripe_auth(), timeout=30)
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ServiceError(f"Stripe session lookup failed: {msg}")
        s = resp.json()
        return {
            "status": s.get("status"),                # open | complete | expired
            "payment_status": s.get("payment_status"),  # paid | unpaid | no_payment_required
            "url": s.get("url"),
        }


def _extract_html(text: str) -> str | None:
    """Pull an HTML document out of a model response."""
    m = re.search(r"```(?:html)?\s*(<!doctype.*?|<html.*?)```", text,
                  flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"<!doctype html.*", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(0).strip()
    m = re.search(r"<html.*</html>", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(0).strip()
    return None


THANKS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thank you!</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;
background:#f6f8fa;color:#1a2330}div{text-align:center;padding:2rem}
h1{font-size:2rem}</style></head>
<body><div><h1>Payment received — thank you!</h1>
<p>Your final website is being prepared and you'll get an email with the live link shortly.</p>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# The agent (state machine)
# ---------------------------------------------------------------------------

class Agent:
    """Drives leads through the pipeline. All transitions are claim-guarded."""

    def __init__(self, db: Database, services: Services, config: dict):
        self.db = db
        self.services = services
        self.config = config
        if hasattr(services, "meter"):
            services.meter = self._spend_google_call

    # -- the money tap -------------------------------------------------------

    def google_calls_this_month(self) -> int:
        try:
            return int(self.db.get_kv(_google_meter_key()) or 0)
        except ValueError:
            return 0

    def _spend_google_call(self) -> None:
        """Count a billable Google call, and refuse once the month's cap is hit.

        Google gives 5,000 Text Search calls a month free and charges about
        $32 per thousand after that. Letting JARVIS work continuously is only
        safe if something says no on the owner's behalf, so this does — the
        default cap sits under the free allowance, and it is the same counter
        whether the calls came from a hunt, a saved search, or a button.
        """
        cap = max(0, setting_int(self.config, "monthly_google_cap",
                                 GOOGLE_CALL_CAP))
        if self.google_calls_this_month() >= cap:
            raise ServiceError(
                "That's %d Google searches this month, which is the cap you're "
                "set to (the free allowance is %d). It resets on the 1st, or "
                "raise the cap in Setup — past the free ones Google charges "
                "about $32 per thousand." % (cap, GOOGLE_FREE_CALLS_MONTH))
        self.db.bump_kv(_google_meter_key())

    def _notify(self, title: str, message: str, priority: str = "default",
                tags: str = "") -> None:
        """Best-effort phone push; never breaks the pipeline."""
        fn = getattr(self.services, "push_notify", None)
        if fn is None:
            return
        try:
            fn(title, message, priority=priority, tags=tags)
        except Exception:
            pass

    # -- lead discovery ----------------------------------------------------

    def find_leads(self, query: str) -> dict:
        found = self.services.places_search(query)
        added = 0
        for p in found:
            if not p.get("place_id"):
                continue
            if self.db.add_lead(**p) is not None:
                added += 1
        r = {"found": len(found), "added": added,
             "seen": getattr(found, "seen", len(found)),
             "with_site": getattr(found, "with_site", 0),
             "closed": getattr(found, "closed", 0),
             "social_only": getattr(found, "social_only", 0),
             "by_status": dict(getattr(found, "by_status", {}) or {})}
        self.db.log(None, "find_leads", f"{query!r}: {describe_search(r)}")
        return r

    def find_businesses(self, area: str = "", trades: str = "",
                        per_trade: int = 20) -> dict:
        """Stock the call sheet for a town.

        Google first, because its phone numbers are the most reliable, then
        OpenStreetMap for anything Google missed — free, no key, and a
        completely separate index. Either source failing is not the search
        failing: whatever the other one found still lands on the sheet.
        """
        area = (area or self.config.get("territory_base") or "").strip()
        if not area:
            return {"ok": False, "error": "No area set — put your town on Setup."}
        wanted = trade_list(trades or self.config.get("trades"))
        added = total = 0
        troubles = []
        for trade in wanted:
            try:
                found = self.services.places_search(f"{trade} in {area}",
                                                    max_results=per_trade)
            except Exception as e:
                troubles.append(f"{trade}: {explain(e, 120)}")
                continue
            total += len(found)
            for row in found:
                if row.get("place_id") and self.db.add_lead(**row) is not None:
                    added += 1
        if not added:
            # Nothing from Google — either no key, a bad area, or the cap. The
            # free index is not a consolation prize; try it properly.
            try:
                point = self.town_centre(area)
                if point:
                    osm = self.services.osm_nearby(
                        point[0], point[1],
                        radius=setting_int(self.config, "find_radius_m", 8000))
                    total += len(osm)
                    for row in osm:
                        if row.get("place_id") and self.db.add_lead(**row) is not None:
                            added += 1
            except Exception as e:
                troubles.append(f"OpenStreetMap: {explain(e, 120)}")
        note = "%d to call added from %s (%d looked at)" % (added, area, total)
        if troubles:
            note += " — " + "; ".join(troubles[:3])
        self.db.log(None, "found_businesses", note[:400],
                    needs_attention=bool(troubles and not added))
        return {"ok": True, "added": added, "seen": total, "area": area,
                "troubles": troubles}

    def top_up_call_sheet(self) -> dict:
        """Go and find more before the sheet runs out.

        Deliberately dull: only when there is genuinely little left to ring,
        and never without an area set.

        The back-off is the important part. An area can simply be exhausted —
        a small town with one trade configured may only ever yield twenty
        businesses, which is below the floor forever. Without this, every tick
        would search again, find the same twenty, add none, and still spend the
        Google calls. That silent repeating spend is exactly what went wrong
        with the app this replaces, so a round that adds nothing buys quiet:
        it will not look again for some hours.
        """
        floor = setting_int(self.config, "call_list_floor", 25)
        if len(self.db.leads_to_call()) >= floor:
            return {"skipped": "sheet is stocked"}
        if not (self.config.get("territory_base") or "").strip():
            return {"skipped": "no area set"}
        rest = setting_int(self.config, "dry_area_rest_hours", 6)
        last_dry = self.db.get_kv("last_dry_search")
        if last_dry and _age_minutes(last_dry) < rest * 60:
            return {"skipped": "nothing new here last time; resting"}
        r = self.find_businesses()
        if r.get("ok") and not r.get("added"):
            # Everything here is already on the sheet. Say so once, then rest.
            self.db.set_kv("last_dry_search", _now())
        else:
            self.db.set_kv("last_dry_search", "")
        return r

    # -- what happened on the call -----------------------------------------

    def mark_called(self, lead_id: int, outcome: str, notes: str = "",
                    call_back_hours: int = 24) -> dict:
        """Record a call. Never sends anything — this is your notepad."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such business."}
        fields = {"last_called_at": _now()}
        if notes.strip():
            fields["call_notes"] = notes.strip()[:2000]
        if outcome == "not_interested":
            self.db.claim(lead_id, [lead["stage"]], STAGE_NOT_INTERESTED)
            said = "Not interested."
        else:
            when = datetime.now(timezone.utc) + timedelta(hours=max(1, call_back_hours))
            fields["call_back_at"] = when.isoformat()
            self.db.claim(lead_id, [lead["stage"]], STAGE_CALL_BACK)
            said = "Call back in %dh." % call_back_hours
        self.db.update_lead(lead_id, **fields)
        self.db.log(lead_id, "called",
                    f"Called {lead['name']}. {said} {notes.strip()[:200]}".strip())
        return {"ok": True}

    def said_yes(self, lead_id: int, email: str, notes: str = "") -> dict:
        """They agreed on the phone and gave you this address.

        This is the only way a lead ever gets an email address in this app, and
        the only way anything is ever sent to one. There is no cold email here:
        every address was read out to you by the person who owns it, which is
        why what follows can happen without asking you again.

        What follows is the part that was already built and already safe: a
        WATERMARKED preview goes out, then a payment link, and the clean site
        is only ever deployed after Stripe confirms the money — checked once
        when the payment lands and again at the moment of delivery.
        """
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such business."}
        email = (email or "").strip()
        if not EMAIL_RE.fullmatch(email):
            return {"ok": False,
                    "error": "That doesn't look like an email address. Read it "
                             "back to them and check — it's the only way to "
                             "reach them now."}
        self.db.update_lead(lead_id, email=email, last_called_at=_now(),
                            email_source="given on the phone",
                            call_notes=(notes.strip()[:2000] or None),
                            error=None, attempts=0)
        if not self.db.claim(lead_id, [STAGE_FOUND, STAGE_CALL_BACK,
                                       STAGE_NOT_INTERESTED],
                             STAGE_BUILDING_PREVIEW):
            return {"ok": False,
                    "error": "That one has already moved on — open it to see "
                             "where it got to."}
        self.db.log(lead_id, "agreed_on_call",
                    f"{lead['name']} said yes on the phone and gave {email}. "
                    f"Building their preview now.")
        self._notify(f"Yes from {lead['name']}",
                     "Building the preview site now — it'll email itself over.",
                     tags="tada")
        self._advance_preview(lead_id)
        return {"ok": True}

    # -- outreach ----------------------------------------------------------

    def process_replies(self) -> dict:
        """Fetch inbound email since the last poll and act on each reply."""
        since = self.db.get_kv("inbound_watermark")
        try:
            inbound = self.services.email_inbound_since(since)
        except Exception as e:
            self.db.log(None, "poll_error",
                        f"Inbound email poll failed: {explain(e, 400)}"[:500])
            return {"ok": False, "error": explain(e)}
        handled = 0
        earliest_skipped = None  # a message we deliberately left for the next poll
        for msg in inbound:
            if self.db.message_seen(msg["message_uuid"]):
                continue
            lead = self.db.find_lead_for_reply(msg["thread_id"], msg["from_address"])
            if lead is None:
                if self.db.mark_message_processed(msg["message_uuid"], None):
                    self.db.log(
                        None, "unmatched_reply",
                        f"Email from {msg['from_address']} ({msg['subject'][:80]!r}) "
                        "doesn't match any lead — handle manually in your inbox.",
                        needs_attention=True)
                    self._notify("Email needs you",
                                 f"From {msg['from_address']}: {msg['subject'][:100]}",
                                 tags="warning")
                continue
            if lead["stage"] in TRANSIENT_STAGES:
                # A step for this lead is mid-flight; leave the message for the
                # next poll rather than racing it.
                if earliest_skipped is None:
                    earliest_skipped = msg["created_at"]
                continue
            # Claim the message itself before acting, so overlapping polls
            # can't process the same reply twice.
            if not self.db.mark_message_processed(msg["message_uuid"], lead["id"]):
                continue
            self._handle_reply(lead, msg)
            handled += 1
        # Advance the watermark, but never past a message we skipped — the
        # processed-message table absorbs the resulting refetch overlap.
        if inbound:
            newest = earliest_skipped or inbound[-1]["created_at"]
            if newest != since:
                self.db.set_kv("inbound_watermark", newest)
        return {"ok": True, "handled": handled}

    def _handle_reply(self, lead: sqlite3.Row, msg: dict) -> None:
        lead_id = lead["id"]
        stage = lead["stage"]
        self.db.log(lead_id, "reply_received",
                    f"Reply from {msg['from_address']}: {msg['snippet'][:120]}")
        self._notify(f"Reply from {lead['name']}", msg["snippet"][:160] or "(no preview)",
                     tags="email")
        try:
            body = self.services.email_fetch_body(msg["message_uuid"]) or msg["snippet"]
        except Exception:
            body = msg["snippet"]

        # Post-payment replies always go to a human.
        if stage in (STAGE_PAID, STAGE_DEPLOYING_FINAL, STAGE_DELIVERED,
                     STAGE_ERROR, STAGE_NOT_INTERESTED, STAGE_FOUND):
            self.db.log(lead_id, "reply_needs_human",
                        f"Reply in stage {stage!r} — reply from your own inbox.",
                        needs_attention=True)
            return

        try:
            intent = self.services.classify_reply(dict(lead), stage, body)
        except Exception as e:
            self.db.log(lead_id, "classify_failed",
                        f"Couldn't classify reply ({explain(e, 300)}) — handle manually.",
                        needs_attention=True)
            return

        if intent == "unsubscribe":
            self.db.update_lead(lead_id, do_not_contact=1)
            self.db.claim(lead_id, [stage], STAGE_NOT_INTERESTED)
            self.db.log(lead_id, "unsubscribed",
                        "Lead asked not to be contacted — flagged do-not-contact.")
            return
        if intent == "declined":
            self.db.claim(lead_id, [stage], STAGE_NOT_INTERESTED)
            self.db.log(lead_id, "declined", "Lead declined.")
            return
        if intent == "unclear":
            self.db.log(lead_id, "reply_unclear",
                        "Reply needs a human answer (question/change request?). "
                        "Reply from your own inbox.", needs_attention=True)
            self._notify(f"{lead['name']} asked something",
                         "Their reply needs a human answer — check your inbox.",
                         tags="warning")
            return

        # intent == interested
        if stage == STAGE_CONTACTED:
            if self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_BUILDING_PREVIEW):
                self.db.update_lead(lead_id, attempts=0,
                                    last_rfc_id=msg["rfc_id"])
                self._advance_preview(lead_id)
        elif stage == STAGE_PREVIEW_SENT:
            if self.db.claim(lead_id, [STAGE_PREVIEW_SENT], STAGE_SENDING_PAYMENT_LINK):
                self.db.update_lead(lead_id, attempts=0,
                                    last_rfc_id=msg["rfc_id"])
                self._advance_payment_link(lead_id)
        elif stage == STAGE_PAYMENT_LINK_SENT:
            self._resend_payment_link(lead_id, msg["rfc_id"])

    # -- preview pipeline (idempotent, resumable) --------------------------

    def _advance_preview(self, lead_id: int) -> None:
        """From stage building_preview: generate HTML -> deploy watermarked
        preview -> email the link -> preview_sent. Each part is skipped if it
        already succeeded, so retries never redo work or double-email."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_BUILDING_PREVIEW:
            return
        try:
            html = lead["site_html"]
            if not html:
                html = self.services.generate_site_html(dict(lead))
                self.db.update_lead(lead_id, site_html=html)
                self.db.log(lead_id, "site_generated",
                            f"Claude designed the site ({len(html)} bytes).")
            if not lead["netlify_url"]:
                deployed = self.services.netlify_deploy(
                    lead["netlify_site_id"], inject_watermark(html),
                    extra_files={"thanks.html": THANKS_HTML})
                self.db.update_lead(lead_id, netlify_site_id=deployed["site_id"],
                                    netlify_url=deployed["url"])
                self.db.log(lead_id, "preview_deployed",
                            f"Watermarked preview live at {deployed['url']}")
            lead = self.db.get_lead(lead_id)
            cfg = self.config
            body = (
                f"Great to hear from you!\n\n"
                f"I went ahead and designed a preview of what your website could look "
                f"like:\n\n    {lead['netlify_url']}\n\n"
                f"It's a watermarked draft — if you like it, just reply and I'll send "
                f"over a secure payment link (${fmt_price(cfg.get('site_price_usd', 500))} flat). "
                f"Once that's done the final, watermark-free site goes live and it's "
                f"all yours.\n\nIf you'd like any changes first, tell me what to tweak."
                f"\n\nBest,\n{cfg.get('your_name') or ''}\n{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Your website preview — {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"], error=None,
                                attempts=0)
            self.db.claim(lead_id, [STAGE_BUILDING_PREVIEW], STAGE_PREVIEW_SENT)
            self.db.log(lead_id, "preview_emailed", "Preview link emailed to lead.")
            self._notify(f"Preview sent — {lead['name']}",
                         f"Watermarked preview is live: {lead['netlify_url']}",
                         tags="art")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_BUILDING_PREVIEW], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_BUILDING_PREVIEW)
                self.db.log(lead_id, "preview_failed",
                            f"Gave up building preview after {attempts} attempts: {explain(e, 300)}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "preview_retry",
                            f"Preview step failed (attempt {attempts}): {explain(e, 400)}"[:500])

    # -- payment link pipeline --------------------------------------------

    def _advance_payment_link(self, lead_id: int) -> None:
        """From stage sending_payment_link: create ONE checkout session ->
        email the link -> payment_link_sent."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_SENDING_PAYMENT_LINK:
            return
        try:
            session_id = lead["stripe_session_id"]
            session_url = lead["stripe_session_url"]
            if not session_id:
                created = self.services.stripe_create_checkout(
                    dict(lead), lead["netlify_url"] or "https://example.com")
                session_id, session_url = created["session_id"], created["url"]
                amount = int(round(float(self.config.get("site_price_usd", 500)) * 100))
                self.db.update_lead(lead_id, stripe_session_id=session_id,
                                    stripe_session_url=session_url,
                                    amount_cents=amount)
                self.db.log(lead_id, "checkout_created",
                            f"Stripe Checkout Session {session_id} created.")
            cfg = self.config
            body = (
                f"Wonderful — glad you like it!\n\n"
                f"Here's your secure payment link for the flat "
                f"${fmt_price(cfg.get('site_price_usd', 500))}:\n\n    {session_url}\n\n"
                f"As soon as the payment goes through, the watermark comes off and "
                f"your final site goes live automatically — you'll get an email with "
                f"the link.\n\nBest,\n{cfg.get('your_name') or ''}\n"
                f"{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Payment link — website for {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"],
                                error=None, attempts=0)
            self.db.claim(lead_id, [STAGE_SENDING_PAYMENT_LINK], STAGE_PAYMENT_LINK_SENT)
            self.db.log(lead_id, "payment_link_emailed", "Payment link emailed.")
            self._notify(f"Payment link sent — {lead['name']}",
                         f"${fmt_price(self.config.get('site_price_usd', 500))} "
                         "checkout link is in their inbox.", tags="link")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_SENDING_PAYMENT_LINK], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_SENDING_PAYMENT_LINK)
                self.db.log(lead_id, "payment_link_failed",
                            f"Gave up sending payment link after {attempts} attempts: {explain(e, 300)}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "payment_link_retry",
                            f"Payment-link step failed (attempt {attempts}): {explain(e, 400)}"[:500])

    def _resend_payment_link(self, lead_id: int, reply_rfc_id: str) -> None:
        """A lead replied after getting the link. Re-send the SAME link if the
        session is still open — never create a second one here."""
        lead = self.db.get_lead(lead_id)
        if lead is None or not lead["stripe_session_id"]:
            return
        try:
            session = self.services.stripe_get_session(lead["stripe_session_id"])
        except Exception as e:
            self.db.log(lead_id, "stripe_poll_error", explain(e, 500), needs_attention=True)
            return
        if session["payment_status"] == "paid":
            return  # payment poller will pick it up
        if session["status"] == "expired":
            self.db.log(lead_id, "checkout_expired",
                        "Lead replied but their payment link expired — use "
                        "'Send new payment link' on the lead page.",
                        needs_attention=True)
            return
        self.db.log(lead_id, "reply_after_link",
                    "Lead replied after getting the payment link — re-sent the same "
                    "link; check the reply in your inbox in case it needs a human "
                    "answer.", needs_attention=True)
        try:
            sent = self.services.email_send(
                to=lead["email"],
                subject=f"Payment link — website for {lead['name']}",
                body_text=(
                    "Just in case it got buried, here's your payment link again:\n\n"
                    f"    {lead['stripe_session_url']}\n\n"
                    "Reply here if you have any questions!"),
                in_reply_to_rfc_id=reply_rfc_id)
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"])
        except Exception as e:
            self.db.log(lead_id, "email_failed", explain(e, 500), needs_attention=True)

    def new_payment_link(self, lead_id: int) -> dict:
        """Manual action: replace an EXPIRED session with a fresh one."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["stage"] != STAGE_PAYMENT_LINK_SENT:
            return {"ok": False, "error": "Lead is not waiting on a payment link."}
        if lead["stripe_session_id"]:
            try:
                session = self.services.stripe_get_session(lead["stripe_session_id"])
            except Exception as e:
                return {"ok": False, "error": str(e)}
            if session["payment_status"] == "paid":
                return {"ok": False, "error": "This lead already paid — no new link needed."}
            if session["status"] != "expired":
                return {"ok": False,
                        "error": "The existing payment link is still valid; re-send that instead."}
        if not self.db.claim(lead_id, [STAGE_PAYMENT_LINK_SENT], STAGE_SENDING_PAYMENT_LINK):
            return {"ok": False, "error": "Lead changed stage; refresh and retry."}
        self.db.update_lead(lead_id, stripe_session_id=None, stripe_session_url=None,
                            attempts=0,
                            checkout_generation=(lead["checkout_generation"] or 0) + 1)
        self._advance_payment_link(lead_id)
        lead = self.db.get_lead(lead_id)
        return {"ok": lead["stage"] == STAGE_PAYMENT_LINK_SENT}

    # -- payment polling + final delivery ----------------------------------

    def poll_payments(self) -> dict:
        """Check Stripe for every lead waiting on payment. THE payment gate:
        'paid' is only ever set here (or in _resend's early return path via
        this same poller), from Stripe's own payment_status."""
        checked = paid = 0
        for lead in self.db.leads_by_stage(STAGE_PAYMENT_LINK_SENT):
            if not lead["stripe_session_id"]:
                continue
            checked += 1
            try:
                session = self.services.stripe_get_session(lead["stripe_session_id"])
            except Exception as e:
                self.db.log(lead["id"], "stripe_poll_error", explain(e, 500))
                continue
            if session["payment_status"] == "paid":
                if self.db.claim(lead["id"], [STAGE_PAYMENT_LINK_SENT], STAGE_PAID):
                    self.db.update_lead(lead["id"], paid_at=_now(), attempts=0)
                    self.db.log(lead["id"], "payment_confirmed",
                                "Stripe confirmed payment. Deploying final site.")
                    amount = fmt_price((lead["amount_cents"] or 0) / 100)
                    self._notify(f"{lead['name']} PAID ${amount}",
                                 "Stripe confirmed the payment — deploying their "
                                 "final site now.", priority="high", tags="moneybag")
                    paid += 1
            elif session["status"] == "expired":
                self.db.log(lead["id"], "checkout_expired",
                            "Payment link expired unpaid — send a new one from the "
                            "lead page if they're still interested.",
                            needs_attention=True)
        # Drive delivery for everything paid (including retries from crashes).
        for lead in self.db.leads_by_stage(STAGE_PAID):
            if self.db.claim(lead["id"], [STAGE_PAID], STAGE_DEPLOYING_FINAL):
                self._advance_delivery(lead["id"])
        return {"ok": True, "checked": checked, "newly_paid": paid}

    def _advance_delivery(self, lead_id: int) -> None:
        """From stage deploying_final: verify payment AGAIN against Stripe,
        deploy the clean site over the preview, email the live link."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_DEPLOYING_FINAL:
            return
        try:
            # Belt-and-braces: re-verify with Stripe at the moment of delivery.
            session = self.services.stripe_get_session(lead["stripe_session_id"])
            if session["payment_status"] != "paid":
                self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_PAYMENT_LINK_SENT)
                self.db.update_lead(lead_id, paid_at=None)
                self.db.log(lead_id, "payment_gate",
                            "Delivery blocked: Stripe no longer reports this session "
                            "as paid.", needs_attention=True)
                return
            if not lead["site_html"]:
                raise ServiceError("No stored site HTML for this lead.")
            deployed = self.services.netlify_deploy(
                lead["netlify_site_id"], lead["site_html"],
                extra_files={"thanks.html": THANKS_HTML})
            self.db.update_lead(lead_id, netlify_site_id=deployed["site_id"],
                                netlify_url=deployed["url"])
            cfg = self.config
            body = (
                f"Payment received — thank you!\n\n"
                f"Your final website is now LIVE (watermark removed):\n\n"
                f"    {deployed['url']}\n\n"
                f"It's all yours. If you ever want changes or a custom domain "
                f"(like www.{re.sub(r'[^a-z0-9]', '', lead['name'].lower())[:20]}.com), "
                f"just reply to this email.\n\n"
                f"Thanks again for your business!\n{cfg.get('your_name') or ''}\n"
                f"{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Your website is live! — {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"],
                                delivered_at=_now(), error=None, attempts=0)
            self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_DELIVERED)
            self.db.log(lead_id, "delivered", f"Final site delivered: {deployed['url']}")
            self._notify(f"Site delivered — {lead['name']}",
                         f"Watermark off, live at {deployed['url']}", tags="tada")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            # NEVER park a paid lead in 'error' silently — roll back to 'paid'
            # so delivery keeps retrying, and flag it loudly.
            self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_PAID)
            self.db.log(lead_id, "delivery_retry",
                        f"Final delivery failed (attempt {attempts}): {explain(e, 300)} — "
                        "they HAVE paid; delivery will retry automatically.",
                        needs_attention=attempts == MAX_ATTEMPTS)

    # -- manual actions (dashboard buttons) --------------------------------

    def set_email(self, lead_id: int, email: str, source: str = None) -> dict:
        """Put an address on a lead. `source` records where it came from when
        JARVIS found it, so the approval screen can show its working."""
        email = (email or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return {"ok": False, "error": "That doesn't look like an email address."}
        self.db.update_lead(lead_id, email=email, email_source=(source or None))
        self.db.log(lead_id, "email_set", f"Email set to {email}")
        return {"ok": True}

    def manual_advance(self, lead_id: int) -> dict:
        """'They're interested' button — same transition the reply classifier
        makes, for when the user judges a reply themselves."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["do_not_contact"]:
            return {"ok": False, "error": "Lead is flagged do-not-contact."}
        if lead["stage"] == STAGE_CONTACTED:
            if self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_BUILDING_PREVIEW):
                self.db.update_lead(lead_id, attempts=0)
                self.db.log(lead_id, "manual_advance", "Marked interested by you.")
                self._advance_preview(lead_id)
                return {"ok": True}
        elif lead["stage"] == STAGE_PREVIEW_SENT:
            if self.db.claim(lead_id, [STAGE_PREVIEW_SENT], STAGE_SENDING_PAYMENT_LINK):
                self.db.update_lead(lead_id, attempts=0)
                self.db.log(lead_id, "manual_advance",
                            "Marked ready for payment link by you.")
                self._advance_payment_link(lead_id)
                return {"ok": True}
        return {"ok": False,
                "error": f"Can't advance from stage {lead['stage']!r} — payment and "
                         "delivery always go through Stripe."}

    def manual_not_interested(self, lead_id: int, do_not_contact: bool = False) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["stage"] in (STAGE_PAID, STAGE_DEPLOYING_FINAL, STAGE_DELIVERED):
            return {"ok": False, "error": "Lead already paid — can't mark not interested."}
        self.db.claim(lead_id, [lead["stage"]], STAGE_NOT_INTERESTED)
        fields = {"do_not_contact": 1} if do_not_contact else {}
        if fields:
            self.db.update_lead(lead_id, **fields)
        self.db.log(lead_id, "manual_not_interested", "Marked not interested by you.")
        return {"ok": True}

    def retry_from_error(self, lead_id: int) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_ERROR:
            return {"ok": False, "error": "Lead is not in the error stage."}
        back_to = lead["stage_before_error"]
        if back_to not in TRANSIENT_STAGES:
            return {"ok": False, "error": "Don't know which step to retry."}
        if not self.db.claim(lead_id, [STAGE_ERROR], back_to):
            return {"ok": False, "error": "Lead changed stage; refresh."}
        self.db.update_lead(lead_id, attempts=0, error=None)
        self.db.log(lead_id, "manual_retry", f"Retrying step {back_to!r}.")
        if back_to == STAGE_BUILDING_PREVIEW:
            self._advance_preview(lead_id)
        elif back_to == STAGE_SENDING_PAYMENT_LINK:
            self._advance_payment_link(lead_id)
        elif back_to == STAGE_DEPLOYING_FINAL:
            self._advance_delivery(lead_id)
        return {"ok": True}

    # -- background tick ---------------------------------------------------

    # -- sweeping one town with everything we have ---------------------------

    def town_centre(self, town: str) -> tuple[float, float] | None:
        """Where a town is: remembered from last time, or looked up once.

        A town is looked up at most once ever — the answer goes in the kv table
        and is reused forever after, so repeat searches of your own area cost
        no Google calls at all."""
        key = "geo:" + town.lower().strip()
        cached = self.db.get_kv(key)
        if cached:
            try:
                lat, lng = cached.split(",")
                return float(lat), float(lng)
            except ValueError:
                pass
        point = self.services.places_geocode(town)
        if point:
            self.db.set_kv(key, "%f,%f" % point)
        return point

    def _kv_int(self, key: str) -> int:
        try:
            return int(self.db.get_kv(key) or 0)
        except (TypeError, ValueError):
            return 0

    def accept_suggested_email(self, lead_id: int) -> dict:
        """Human accepts the Researcher's suggestion for a lead."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if not lead["suggested_email"]:
            return {"ok": False, "error": "No suggestion to accept."}
        result = self.set_email(lead_id, lead["suggested_email"],
                                source=lead["suggested_email_source"])
        if result.get("ok"):
            self.db.update_lead(lead_id, suggested_email=None,
                                suggested_email_source=None,
                                suggested_email_note=None)
        return result

    def reject_suggested_email(self, lead_id: int) -> dict:
        self.db.update_lead(lead_id, suggested_email=None,
                            suggested_email_source=None,
                            suggested_email_note=None)
        self.db.log(lead_id, "email_rejected", "You rejected the suggested email.")
        return {"ok": True}

    def tick_transients(self) -> None:
        """Resume any lead parked in a transient stage (e.g. after a crash or
        a failed attempt). Each _advance_* step is idempotent."""
        for lead in self.db.leads_by_stage(STAGE_BUILDING_PREVIEW):
            self._advance_preview(lead["id"])
        for lead in self.db.leads_by_stage(STAGE_SENDING_PAYMENT_LINK):
            self._advance_payment_link(lead["id"])
        for lead in self.db.leads_by_stage(STAGE_DEPLOYING_FINAL):
            self._advance_delivery(lead["id"])

    def tick(self) -> None:
        """One background round.

        This app never sends a cold email, so nothing here contacts a stranger.
        Every address it writes to was given to you over the phone. What runs
        in the background is only the work that follows a "yes": reading the
        replies to a preview you sent, watching Stripe, finishing a half-built
        site — plus keeping the call sheet stocked so you always have someone
        to ring.

        Each step runs on its own. One step throwing used to take the rest of
        the round down with it, so a mailbox problem quietly stopped everything
        else too.
        """
        mailbox = bool(self.config.get("inkbox_api_key"))
        for name, needed, step in (
                ("reading replies", mailbox, self.process_replies),
                ("checking payments", True, self.poll_payments),
                ("finishing half-done work", True, self.tick_transients),
                ("stocking the call sheet", True, self.top_up_call_sheet)):
            if not needed:
                continue
            try:
                step()
            except Exception as e:
                self.db.log(None, "tick_failed",
                            f"{name}: {explain(e, 250)}"[:400])
