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
from datetime import date, datetime, timezone
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
DAILY_LINES = [
    ("The tenth no is closer to a yes than the first one was.", ""),
    ("A business with no website is not a hard sell. It's an obvious one.", ""),
    ("Send the email you'd want to receive.", ""),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    ("Amateurs sit and wait for inspiration, the rest of us just get up and "
     "go to work.", "Stephen King, On Writing"),
    ("Twenty good emails beat two hundred lazy ones.", ""),
    ("The quiet weeks are the ones that decide it.", ""),
    ("You are not interrupting them. You are offering to fix something "
     "they already know is broken.", ""),
    ("Every studio you admire started with one client who said yes.", ""),
    ("Slow is smooth. Smooth is fast.", ""),
    ("A no today is a maybe next spring. Keep the list.", ""),
    ("Design the site as if they've already paid for it.", ""),
    ("The work you do before anyone is watching is the work.", ""),
    ("Nobody was ever talked into caring. They were shown.", ""),
    ("Ship it slightly before you feel ready.", ""),
    ("If you can't write the email in five sentences, you don't know the "
     "offer well enough yet.", ""),
    ("Being early is a kind of advantage nobody can copy.", ""),
    ("Small and finished beats big and pending.", ""),
    ("The person who replies at 11pm is the person who wants it.", ""),
    ("Charge for the outcome, not the hours.", ""),
    ("A well-run day looks boring from the outside.", ""),
    ("Do the follow-up. That's where the money is.", ""),
    ("Your first ten clients teach you what to sell to the next hundred.", ""),
    ("Make it easy to say yes and impossible to misunderstand.", ""),
    ("Consistency is a skill, not a personality trait.", ""),
    ("The gap between the work you make and the work you want to make "
     "closes by making more work.", ""),
    ("Don't polish the pitch. Polish the thing you're pitching.", ""),
    ("Answer fast. Speed reads as competence.", ""),
    ("The best time to plant a tree was twenty years ago. "
     "The second best time is now.", "Proverb"),
    ("You only need this to work once to know it works.", ""),
    ("Rejection is information, not a verdict.", ""),
    ("Build the boring machine that runs while you sleep.", ""),
    ("One good local business tells three others.", ""),
    ("A deadline you set for yourself still counts.", ""),
    ("Some days the job is just: send the next one.", ""),
    ("Price it so you'd be glad to do it again.", ""),
    ("You can't control the reply. You can control the sending.", ""),
    ("Make something a stranger would pay for. Then find the stranger.", ""),
    ("Finish something today, even if it's small.", ""),
    ("The compounding is invisible right up until it isn't.", ""),
]


def line_for_today(today: date | None = None) -> dict:
    """The same line all day, a different one tomorrow, cycling the whole list
    before any of it comes round again."""
    day = today or date.today()
    text, source = DAILY_LINES[day.toordinal() % len(DAILY_LINES)]
    return {"text": text, "source": source}


# Trades where a lot of businesses still have no website. Restaurants, salons
# and gyms are deliberately absent — nearly all of them have one, so searching
# for them burns Google calls to find nobody.
# Google Text Search: 5,000 calls a month free, then roughly $32 per thousand.
# The cap sits under the free line on purpose.
GOOGLE_FREE_CALLS_MONTH = 5000
GOOGLE_CALL_CAP = 4500


def _google_meter_key() -> str:
    return "google_calls_" + datetime.now(timezone.utc).strftime("%Y-%m")


DEFAULT_TRADES = [
    "plumbers", "electricians", "landscapers", "tree service", "roofers",
    "handyman", "house cleaning", "towing", "junk removal", "septic service",
    "masonry", "excavation", "snow plowing", "small engine repair",
    "auto repair", "barber shops", "moving companies", "pest control",
    "HVAC", "fencing contractors",
]


def call_opener(lead: dict, cfg: dict) -> str:
    """A short thing to say when they answer. Plain, honest, and no API needed.

    Deliberately not generated: it should read the same every time so it can be
    practised, and it has to work before any keys are in.
    """
    name = (cfg.get("your_name") or "").strip() or "me"
    studio = (cfg.get("studio_name") or "Solo Studio").strip()
    price = fmt_price(cfg.get("site_price_usd", 500))
    business = lead.get("name") or "your business"
    return (
        f"Hi, is this {business}? My name's {name}, I run {studio} — I build "
        f"websites for local businesses.\n\n"
        f"I noticed you don't have a website up. I can put together a simple "
        f"one-page site for a flat ${price} — no monthly fee.\n\n"
        f"What I'd normally do is design it first so you can see it, and you "
        f"only pay if you like it. What's the best email to send it to?"
    )


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

STAGE_FOUND = "found"                      # discovered via Places, may lack email
STAGE_CONTACTED = "contacted"              # cold email sent, waiting for reply
STAGE_BUILDING_PREVIEW = "building_preview"    # transient: generating + deploying preview
STAGE_PREVIEW_SENT = "preview_sent"        # watermarked preview emailed
STAGE_SENDING_PAYMENT_LINK = "sending_payment_link"  # transient
STAGE_PAYMENT_LINK_SENT = "payment_link_sent"  # checkout link emailed, polling Stripe
STAGE_PAID = "paid"                        # Stripe verified paid; delivery pending
STAGE_DEPLOYING_FINAL = "deploying_final"  # transient: deploying clean site
STAGE_DELIVERED = "delivered"              # clean site live + link emailed
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
    "yelp_api_key": "",       # a fourth index of local businesses
    "hunter_api_key": "",     # addresses behind a domain we already know
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
    "autopilot_enabled": False,   # background processing of replies/payments
    "poll_interval_seconds": 60,
    # Automatic prospecting: the agent runs these searches on a schedule and
    # queues what it finds for your approval. It never emails anyone on its own.
    "auto_search_enabled": False,
    "auto_research_enabled": True,    # let the Researcher hunt missing emails
    "research_per_tick": 3,           # how many leads to research each round
    # An address the Researcher found goes straight onto the lead instead of
    # queueing for a second click. It changes nothing about sending: the cold
    # email itself is still read and approved by a person, with that address
    # shown, so a wrong one is caught before anything leaves.
    "auto_accept_emails": True,
    # How wide to cast the net: "none" only takes businesses with no website
    # at all, "broken" adds dead links, parked domains and social-only pages,
    # "weak" adds sites that are http-only or unusable on a phone.
    "lead_quality": "broken",
    "lead_floor": 15,                 # hunt when fewer than this are waiting
    "hunt_interval_hours": 6,         # and no more often than this
    "monthly_google_cap": GOOGLE_CALL_CAP,
    "saved_searches": "",         # one search per line
    "search_interval_hours": 12,
    # 20 searches x 3 pages, twice a day, is ~3,600 Google calls a month —
    # inside the 5,000 free ones, and enough ground per run to actually turn
    # something up. The Setup page projects the cost of any other number.
    "searches_per_run": 20,
    "territory_base": "",         # e.g. "Napanoch, NY"
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
    social_url TEXT,        -- their Facebook/Instagram page, when that's all they have
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
                          ("social_url", "TEXT"),
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

    def leads_awaiting_approval(self) -> list[sqlite3.Row]:
        """Found leads with an email, ready for you to approve."""
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND email IS NOT NULL AND email<>'' ORDER BY created_at",
            (STAGE_FOUND,)).fetchall()

    def leads_to_research(self, limit: int = 3) -> list[sqlite3.Row]:
        """Found leads with no email and no suggestion yet, never researched."""
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND (email IS NULL OR email='')"
            " AND (suggested_email IS NULL OR suggested_email='')"
            " AND researched_at IS NULL ORDER BY created_at LIMIT ?",
            (STAGE_FOUND, limit)).fetchall()

    def leads_to_call(self) -> list[sqlite3.Row]:
        """Businesses with a phone number that outreach hasn't reached yet.

        Whoever you haven't tried yet comes first, so the top of the list is
        never someone you rang two minutes ago. Within that, the ones with no
        email address lead — email can't reach them at all, so a call is the
        only way that lead ever goes anywhere. Then longest-since-tried.
        """
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND phone IS NOT NULL AND phone != ''"
            " ORDER BY last_called_at IS NOT NULL ASC,"
            "          (email IS NOT NULL AND email != '') ASC,"
            "          last_called_at ASC, id ASC",
            (STAGE_FOUND,)).fetchall()

    def leads_needing_email(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND (email IS NULL OR email='') ORDER BY created_at",
            (STAGE_FOUND,)).fetchall()

    def distinct_replied_leads(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM events"
            " WHERE kind='reply_received' AND lead_id IS NOT NULL").fetchone()
        return int(row["n"] or 0)

    # -- leads ------------------------------------------------------------

    def add_lead(self, *, place_id, name, address, phone, category, email=None,
                 social_url=None, site_status=None, site_note=None) -> int | None:
        """Insert a lead; returns new id, or None if this place already exists."""
        c = self._conn()
        try:
            with c:
                cur = c.execute(
                    "INSERT INTO leads (place_id, name, address, phone, category, email,"
                    " social_url, site_status, site_note, stage, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (place_id, name, address, phone, category, email, social_url,
                     site_status, site_note, STAGE_FOUND, _now(), _now()),
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
}

# How wide to cast the net, in order. Each level adds to the one before it.
QUALITY_LEVELS = {
    "none": (SITE_NONE,),
    "broken": (SITE_NONE, SITE_SOCIAL, SITE_DEAD, SITE_PARKED),
    "weak": (SITE_NONE, SITE_SOCIAL, SITE_DEAD, SITE_PARKED,
             SITE_INSECURE, SITE_NOT_MOBILE),
}

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


def _place_row(p: dict) -> dict:
    """One Google place, in the shape a lead is stored in."""
    kind = p.get("primaryTypeDisplayName")
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", "Unknown"),
        "address": p.get("formattedAddress"),
        "phone": p.get("nationalPhoneNumber"),
        "category": kind.get("text") if isinstance(kind, dict) else kind,
        "social_url": p.get("websiteUri") or None,
    }


def _yelp_row(b: dict) -> dict | None:
    """One Yelp business, in the shape a lead is stored in.

    The url is their Yelp page, not their website — which is exactly why it
    lands in the "only a social/directory page" class.
    """
    name = (b.get("name") or "").strip()
    if not name or b.get("is_closed"):
        return None
    loc = b.get("location") or {}
    address = ", ".join(x for x in (loc.get("display_address") or []) if x)
    cats = ", ".join(c.get("title", "") for c in (b.get("categories") or [])
                     if c.get("title"))
    return {
        "place_id": "yelp:" + str(b.get("id") or name),
        "name": name,
        "address": address or None,
        "phone": b.get("display_phone") or b.get("phone") or None,
        "category": cats or None,
        "social_url": b.get("url") or "https://yelp.com",
    }


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
    return {
        "place_id": "osm:%s/%s" % (el.get("type"), el.get("id")),
        "name": name,
        "address": address or None,
        "phone": (tags.get("phone") or tags.get("contact:phone")
                  or tags.get("contact:mobile") or None),
        "category": trade.replace("_", " ").title() or None,
        "social_url": (tags.get("website") or tags.get("contact:website")
                       or tags.get("contact:facebook") or None),
    }


class SearchResults(list):
    """Leads found, plus what was passed over on the way there.

    A search that comes back empty is not the same as a search that went
    wrong, and neither is the same as a search whose every result already sits
    in the database. Carrying the counts means the app can say which.
    """

    def __init__(self, *a):
        super().__init__(*a)
        self.seen = 0            # businesses Google returned
        self.with_site = 0       # rejected: a real, working, modern website
        self.closed = 0          # rejected: permanently closed
        self.social_only = 0     # kept: only a Facebook/Instagram/Yelp page
        self.by_status = {}      # kept, counted by what's wrong with their site


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


def checkup(db, cfg, key_fields=API_KEYS, extra=()) -> list[dict]:
    """Everything currently worth telling the owner, worst first."""
    out = []

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

    recent = " ".join((e["detail"] or "") for e in db.recent_events(40))
    if "$0 of API credit" in recent or "credit balance is too low" in recent:
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
    waiting = db.leads_awaiting_approval()
    if waiting:
        out.append(finding(
            "approve-queue", WAITING,
            "%d cold email%s waiting for you" % (len(waiting),
                                                 "" if len(waiting) == 1 else "s"),
            "Nothing goes out until you read it and press send.",
            "/approve", "Review them"))

    need_email = db.leads_needing_email()
    if need_email:
        out.append(finding(
            "need-email", WAITING,
            "%d lead%s with no email address" % (len(need_email),
                                                 "" if len(need_email) == 1 else "s"),
            "They can't be contacted until one is found. The Researcher can "
            "suggest them, or you can call instead.", "/calls", "Call them"))

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

    # -- switched off -------------------------------------------------------
    if not cfg.get("autopilot_enabled"):
        out.append(finding(
            "autopilot-off", WATCH, "Autopilot is off",
            "Replies and payments are only checked when you press a button.",
            "/setup", "Turn it on"))
    elif not cfg.get("auto_search_enabled"):
        out.append(finding(
            "search-off", WATCH, "Automatic lead hunting is off",
            "No new leads will appear on their own.", "/setup", "Turn it on"))
    elif not (cfg.get("saved_searches") or "").strip():
        out.append(finding(
            "no-searches", WATCH, "No saved searches",
            "Put your town in on Setup and it builds the list for you.",
            "/setup", "Build the list"))

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

    def places_search_no_website(self, query: str,
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
                    "social_url": p.get("websiteUri") or None,
                })
            page_token = data.get("nextPageToken")
            if not page_token or len(raw) >= max_results:
                break

        return self._triage(raw, results, max_results)

    def _triage(self, raw: list[dict], results: "SearchResults",
                max_results: int) -> "SearchResults":
        """Look at every website and keep the businesses worth pitching.

        Every source — Google's text search, Google by distance, OpenStreetMap
        — comes through here, so a lead means the same thing whichever index
        it was found in.
        """
        keep = QUALITY_LEVELS.get(
            self.config.get("lead_quality", "broken"), QUALITY_LEVELS["broken"])
        for lead, (status, note) in zip(raw, self._check_sites(raw)):
            if status not in keep:
                results.with_site += 1
                continue
            lead["site_status"] = status
            lead["site_note"] = note[:200] if note else None
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
                "places.primaryTypeDisplayName", "places.businessStatus"]),
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

    # -- Yelp, for coverage Google and OSM both miss -------------------------

    YELP_URL = "https://api.yelp.com/v3/businesses/search"
    YELP_MAX = 50

    def yelp_nearby(self, lat: float, lng: float, radius: int = 5000,
                    term: str = None, max_results: int = 50) -> SearchResults:
        """Local businesses from Yelp.

        Worth being straight about what this can and can't do: Yelp's search
        returns a business's Yelp page, never its own website. So Yelp can
        tell us a business exists, with a phone number, but not whether it
        already has a site. Everything from here is therefore treated as
        "only a Yelp page found" — a real lead under the normal setting, and
        a weaker one than Google or OSM, where we checked the actual site.
        """
        key = (self.config.get("yelp_api_key") or "").strip()
        if not key:
            raise ServiceError("No Yelp key set (it's optional — see Setup).")
        params = {"latitude": lat, "longitude": lng,
                  "radius": int(max(1, min(40000, radius))),
                  "limit": max(1, min(self.YELP_MAX, max_results)),
                  "sort_by": "distance"}
        if term:
            params["term"] = term
        try:
            resp = requests.get(self.YELP_URL, params=params, timeout=30,
                                headers={"Authorization": "Bearer " + key})
        except requests.RequestException as e:
            raise ServiceError(f"Couldn't reach Yelp: {e}") from e
        if resp.status_code == 401:
            raise ServiceError("Yelp didn't accept that key. Check it on Setup.")
        if resp.status_code == 429:
            raise ServiceError("Yelp's daily limit is used up — it resets "
                               "tomorrow. Everything else is unaffected.")
        if resp.status_code != 200:
            raise ServiceError(
                f"Yelp error {resp.status_code}: {resp.text[:200]}")
        raw = []
        for b in (resp.json().get("businesses") or []):
            row = _yelp_row(b)
            if row:
                raw.append(row)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    # -- Hunter, for addresses behind a domain we already know ---------------

    def hunter_email(self, domain: str) -> dict:
        """Ask Hunter for a public address at a domain.

        Only useful where a domain is known — which, for this app, means the
        businesses whose site is dead or parked. A business with no website at
        all has no domain to ask about, and those still go to Claude's web
        search.
        """
        key = (self.config.get("hunter_api_key") or "").strip()
        if not key:
            raise ServiceError("No Hunter key set (it's optional — see Setup).")
        try:
            resp = requests.get(
                "https://api.hunter.io/v2/domain-search", timeout=30,
                params={"domain": domain, "api_key": key, "limit": 5})
        except requests.RequestException as e:
            raise ServiceError(f"Couldn't reach Hunter: {e}") from e
        if resp.status_code in (401, 403):
            raise ServiceError("Hunter didn't accept that key. Check it on Setup.")
        if resp.status_code == 429:
            raise ServiceError("Hunter's monthly quota is used up.")
        if resp.status_code != 200:
            raise ServiceError(
                f"Hunter error {resp.status_code}: {resp.text[:200]}")
        data = (resp.json().get("data") or {})
        best = None
        for row in (data.get("emails") or []):
            value = (row.get("value") or "").strip()
            if not value:
                continue
            # Prefer a generic business address over a named person's.
            generic = (row.get("type") or "") == "generic"
            score = (2 if generic else 0) + (1 if row.get("confidence", 0) >= 70 else 0)
            if best is None or score > best[0]:
                best = (score, value, row)
        if not best:
            return {"found": False, "note": f"Hunter had nothing for {domain}."}
        _, email, row = best
        return {"found": True, "email": email,
                "source": f"https://hunter.io (domain search on {domain})",
                "note": "Hunter, confidence %s%%" % row.get("confidence", "?")}

    # -- OpenStreetMap, which costs nothing at all ---------------------------

    # Overpass is a free service run by volunteers. Be a good guest: one
    # request at a time, a real User-Agent, a bounded query, and a cap on how
    # much is asked for.
    # Several mirrors, because a free volunteer service is allowed to be busy.
    OVERPASS_URLS = ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter",
                     "https://overpass.osm.ch/api/interpreter")
    OVERPASS_MAX = 200
    OSM_SELECTORS = (
        'nwr["shop"]["name"]',
        'nwr["craft"]["name"]',
        'nwr["office"]["name"]',
        'nwr["amenity"~"^(car_repair|car_wash|veterinary|dentist|doctors|'
        'driving_school|childcare|bar|cafe|pharmacy|fuel)$"]["name"]',
    )

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
        urls = [(lead.get("social_url") or "") for lead in leads]
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
        interested | declined | unsubscribe | unclear."""
        client = self._get_anthropic()
        model = self.config.get("anthropic_model") or "claude-opus-5"
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
            model=model, max_tokens=2000,
            output_config={"effort": "low"},
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
        model = self.config.get("anthropic_model") or "claude-opus-5"
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if m.get("content")]
        if not messages:
            raise ServiceError("Nothing to answer.")
        with client.messages.stream(
            model=model,
            max_tokens=8000,
            output_config={"effort": "medium"},
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

    def towns_near(self, base: str, miles: int) -> list[str]:
        """Ask Claude for the real towns within `miles` of `base`.

        This is the bit a person shouldn't have to do by hand — nobody knows
        every hamlet in their county, and typing them one at a time is how the
        automatic search ends up never being switched on.
        """
        client = self._get_anthropic()
        model = self.config.get("anthropic_model") or "claude-opus-5"
        prompt = (
            f"List the towns, villages and hamlets within about {miles} miles "
            f"of {base}.\n\n"
            "Rules:\n"
            "- Real inhabited places only, the kind that have local trades in "
            "them and that people use as a postal address. No townships nobody "
            "names.\n"
            "- Around a big city, its separate suburbs and small incorporated "
            "cities count, and are wanted.\n"
            "- SMALLEST first, biggest last. This is for a one-person web "
            "designer looking for businesses that never got a website, and in "
            "a city centre every business already has one. The small places "
            "are the whole point.\n"
            "- Include the starting place itself, but put it last.\n"
            "- At most 25.\n"
            '- Format each as "Town, ST" on its own line. Nothing else — no '
            "numbering, no commentary, no blank lines."
        )
        response = client.messages.create(
            model=model, max_tokens=4000,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": 4}],
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise ServiceError("Couldn't work that area out — try a nearby town.")
        text = "".join(b.text for b in response.content if b.type == "text")
        towns, seen = [], set()
        for line in text.splitlines():
            line = line.strip().lstrip("-•*0123456789. ").strip()
            # a town line looks like "Ellenville, NY" and nothing more
            if not re.fullmatch(r"[A-Za-z .'\-]{2,40},\s*[A-Za-z]{2,20}", line):
                continue
            key = line.lower()
            if key not in seen:
                seen.add(key)
                towns.append(line)
        if not towns:
            raise ServiceError(
                f"Couldn't find any towns near {base!r}. Check the spelling — "
                'it wants something like "Napanoch, NY".')
        return towns[:25]

    def research_email(self, lead: dict) -> dict:
        """Look up a business's public contact email using Claude's web search.

        Returns {"found": bool, "email": str, "source": str, "note": str}.
        Only ever *suggests* — a human confirms before anything is emailed.
        """
        client = self._get_anthropic()
        model = self.config.get("anthropic_model") or "claude-opus-5"
        who = [f"Business name: {lead['name']}"]
        if lead.get("address"):
            who.append(f"Address: {lead['address']}")
        if lead.get("phone"):
            who.append(f"Phone: {lead['phone']}")
        if lead.get("category"):
            who.append(f"Type: {lead['category']}")
        # When Google gave us their Facebook page instead of a website, hand it
        # over: it is usually the one page on the internet with their email.
        if lead.get("social_url"):
            who.append(f"Their only web presence: {lead['social_url']}")
        prompt = (
            "Find the public contact email address for this specific local "
            "business:\n\n" + "\n".join(who) + "\n\n"
            "It has no website of its own, so check places like its Facebook "
            "page, Yelp or Google listing, a directory, or a chamber-of-commerce "
            "page.\n\n"
            "Rules:\n"
            "- Only report an address you actually saw on a page, with the URL.\n"
            "- It must clearly belong to THIS business (match the address or "
            "phone number above), not a similarly named one elsewhere.\n"
            "- Never invent or guess an address, and never construct one from "
            "the business name. If you can't find one, say so.\n"
            "- Prefer a direct business address over a generic contact form.\n\n"
            "Finish your reply with one final line in exactly this format:\n"
            "RESULT: <email> | <url where you saw it> | <short note>\n"
            "or, if you could not find one:\n"
            "RESULT: none | | <short reason>"
        )
        try:
            response = client.messages.create(
                model=model, max_tokens=8000,
                output_config={"effort": "low"},
                tools=[{"type": "web_search_20260209", "name": "web_search",
                        "max_uses": 5}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise ServiceError(f"Email research failed: {explain(e)}") from e
        if response.stop_reason == "refusal":
            return {"found": False, "note": "Claude declined this lookup."}
        text = "".join(b.text for b in response.content if b.type == "text")
        line = ""
        for candidate in reversed(text.splitlines()):
            if candidate.strip().upper().startswith("RESULT:"):
                line = candidate.strip()[len("RESULT:"):].strip()
                break
        if not line:
            return {"found": False, "note": "No usable answer from the search."}
        parts = [p.strip() for p in line.split("|")]
        email = parts[0] if parts else ""
        source = parts[1] if len(parts) > 1 else ""
        note = parts[2] if len(parts) > 2 else ""
        if (not email or email.lower() == "none"
                or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)):
            return {"found": False, "note": note or "No email found online."}
        return {"found": True, "email": email, "source": source, "note": note}

    # -- phone push notifications (ntfy.sh) --------------------------------

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
        cap = int(self.config.get("monthly_google_cap", GOOGLE_CALL_CAP)
                  or GOOGLE_CALL_CAP)
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
        found = self.services.places_search_no_website(query)
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

    # -- outreach ----------------------------------------------------------

    def render_outreach(self, lead) -> dict:
        """The exact subject/body that would be sent — used by the approval
        screen so nothing goes out unseen."""
        cfg = self.config
        fmt = {
            "lead_name": lead["name"],
            "your_name": cfg.get("your_name") or "the owner",
            "studio_name": cfg.get("studio_name") or "Solo Studio",
            "price": fmt_price(cfg.get("site_price_usd", 500)),
            "mailing_address": cfg.get("mailing_address") or "",
        }
        try:
            subject = (cfg.get("outreach_subject", "").format(**fmt)
                       or f"A website for {lead['name']}")
            body = cfg.get("outreach_body", "").format(**fmt)
        except (KeyError, IndexError, ValueError) as e:
            return {"ok": False,
                    "error": f"Your email template has a bad placeholder: {e}. "
                             "Fix it on the Setup page."}
        return {"ok": True, "subject": subject, "body": body}

    def send_outreach(self, lead_id: int) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["do_not_contact"]:
            return {"ok": False, "error": "Lead is flagged do-not-contact."}
        if not lead["email"]:
            return {"ok": False, "error": "Lead has no email address yet."}
        cap = int(self.config.get("daily_send_cap", 20) or 0)
        if cap and self.db.sends_today() >= cap:
            return {"ok": False,
                    "error": f"Daily limit reached ({cap} cold emails today). "
                             "This protects your sending reputation — the rest "
                             "stay queued for tomorrow."}
        rendered = self.render_outreach(lead)
        if not rendered["ok"]:
            return rendered
        # Claim so a double-clicked button can't send two cold emails.
        if not self.db.claim(lead_id, [STAGE_FOUND], STAGE_CONTACTED):
            return {"ok": False, "error": "Lead is not in 'found' stage."}
        try:
            sent = self.services.email_send(to=lead["email"],
                                            subject=rendered["subject"],
                                            body_text=rendered["body"])
        except Exception as e:
            # Roll back so the lead can be retried.
            self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_FOUND)
            self.db.update_lead(lead_id, error=explain(e, 500))
            self.db.log(lead_id, "outreach_failed", explain(e, 500), needs_attention=True)
            return {"ok": False, "error": explain(e)}
        self.db.update_lead(
            lead_id, thread_id=sent["thread_id"],
            last_rfc_id=sent["rfc_id"], error=None)
        self.db.log(lead_id, "outreach_sent", f"Cold email sent to {lead['email']}")
        return {"ok": True}

    # -- inbound replies ---------------------------------------------------

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
        """Where a town is, looked up once and remembered forever."""
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

    def sweep_town(self, town: str, radius: int = 5000) -> dict:
        """Everything we can find in one town, from every index we have.

        Three passes that fail independently, because they fail for different
        reasons: Google by distance (the nearest businesses rather than the
        best known), and OpenStreetMap (free, no key, a different map of the
        world altogether). Either one being down must not stop the other.
        """
        added = seen = 0
        notes = []
        point = None
        try:
            point = self.town_centre(town)
        except Exception as e:
            notes.append(f"couldn't place {town}: {explain(e, 90)}")
        if not point:
            return {"added": 0, "seen": 0, "notes": notes or ["no location"]}
        lat, lng = point

        sources = [
            ("Google by distance",
             lambda: self.services.places_nearby(lat, lng, radius=radius)),
            ("OpenStreetMap",
             lambda: self.services.osm_nearby(lat, lng, radius=radius)),
        ]
        if (self.config.get("yelp_api_key") or "").strip():
            sources.append(("Yelp",
                            lambda: self.services.yelp_nearby(lat, lng,
                                                              radius=radius)))
        for label, call in sources:
            try:
                found = call()
            except Exception as e:
                notes.append(f"{label}: {explain(e, 90)}")
                self.db.log(None, "sweep_failed", f"{label} in {town}: "
                            f"{explain(e, 200)}"[:400])
                continue
            seen += getattr(found, "seen", len(found))
            new = 0
            for lead in found:
                if not lead.get("place_id"):
                    continue
                if self.db.add_lead(**lead) is not None:
                    new += 1
            added += new
            self.db.log(None, "sweep",
                        f"{label} around {town}: {getattr(found, 'seen', 0)} "
                        f"businesses, {len(found)} worth pitching, {new} new.")
        return {"added": added, "seen": seen, "notes": notes}

    # -- the hunt ----------------------------------------------------------

    def hunt(self, area: str, want: int = 8, budget: int = 12) -> dict:
        """Go and find leads in an area without being told what to look for.

        Given nothing but "Los Angeles", work out the towns around it and the
        trades worth trying, then keep searching until enough new leads turn
        up or the budget of searches runs out. Stops the moment it has enough,
        so a good area costs two or three searches, not twelve.

        Why this exists: typing a city into a business search returns the
        city, and the biggest, best-known firms in it — every one of which has
        a website. Nobody would guess that from an empty result, and the
        honest answer ("search smaller places, one trade at a time") is work
        the app should be doing itself.
        """
        area = (area or "").strip()
        if not area:
            return {"ok": False, "added": 0, "summary": "No area given."}

        towns, town_note = [], ""
        try:
            miles = int(self.config.get("territory_miles", 30) or 30)
            towns = self.services.towns_near(area, miles)
        except Exception as e:
            town_note = ("Couldn't work out the towns nearby (%s), so this "
                         "searched %s itself." % (explain(e, 120), area))
        if area not in towns:
            towns.append(area)          # the place they asked for, searched last

        trades = [t.strip() for t in
                  (self.config.get("trades") or "").splitlines() if t.strip()]
        trades = trades or list(DEFAULT_TRADES)

        # Same rotation the saved list uses: sweep the towns, changing trade
        # each pass, so a short hunt covers ground instead of one postcode.
        queries = [f"{trades[(j + k) % len(trades)]} in {towns[j]}"
                   for k in range(len(trades)) for j in range(len(towns))]

        added = seen = ran = 0
        best = []
        swept = set()
        for query in queries[:budget]:
            ran += 1
            where = query.split(" in ", 1)[-1]

            # First time in this town, take everything: the nearest businesses
            # rather than the best-known ones, and OpenStreetMap's own map of
            # the place, which costs nothing at all.
            if where not in swept:
                swept.add(where)
                try:
                    sweep = self.sweep_town(where)
                    added += sweep["added"]
                    seen += sweep["seen"]
                    if sweep["added"]:
                        best.append(f"{sweep['added']} around {where}")
                except Exception as e:
                    self.db.log(None, "hunt_failed",
                                f"sweep of {where}: {explain(e, 200)}"[:400])
                if added >= want:
                    break

            try:
                r = self.find_leads(query)
            except Exception as e:
                self.db.log(None, "hunt_failed",
                            f"{query!r}: {explain(e, 250)}"[:400])
                continue
            added += r["added"]
            seen += r["seen"]
            if r["added"]:
                best.append(f"{r['added']} in {where}")
            if added >= want:
                break

        if added:
            summary = ("Found %d new lead%s — %s. Looked at %d businesses "
                       "across %d search%s."
                       % (added, "" if added == 1 else "s", ", ".join(best[:4]),
                          seen, ran, "" if ran == 1 else "es"))
        elif seen:
            summary = ("No luck: %d businesses across %d searches near %s and "
                       "they all had websites already. Try a different trade, "
                       "or somewhere further out."
                       % (seen, ran, area))
        else:
            summary = ("Google returned nothing at all for %s — check the "
                       "spelling, and include the state." % area)
        if town_note:
            summary += " " + town_note
        self.db.log(None, "hunt", summary)
        if added:
            self._notify("%d new leads found" % added,
                         "JARVIS went hunting near %s." % area, tags="mag")
        return {"ok": True, "added": added, "seen": seen, "searched": ran,
                "towns": len(towns), "summary": summary}

    def run_saved_searches(self, force: bool = False) -> dict:
        """Run the saved searches if they're due, queuing what's found for
        approval. Never emails anyone — discovery only."""
        cfg = self.config
        if not force and not cfg.get("auto_search_enabled"):
            return {"ok": True, "skipped": "auto search off"}
        queries = [q.strip() for q in (cfg.get("saved_searches") or "").splitlines()
                   if q.strip()]
        if not queries:
            return {"ok": True, "skipped": "no saved searches"}
        interval = max(1, int(cfg.get("search_interval_hours", 12) or 12)) * 3600
        last = self.db.get_kv("last_auto_search")
        if not force and last:
            try:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last)).total_seconds()
                if elapsed < interval:
                    return {"ok": True, "skipped": "not due yet"}
            except ValueError:
                pass
        self.db.set_kv("last_auto_search", _now())

        # Google bills per search, and the free allowance is 5,000 calls a
        # month. So a run spends a fixed budget and picks up where it left off
        # next time, working round the list instead of running all of it every
        # time — a hundred saved searches on a 12-hour loop would otherwise be
        # hundreds of dollars a month.
        per_run = max(1, int(cfg.get("searches_per_run", 20) or 20))
        try:
            cursor = int(self.db.get_kv("search_cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        cursor %= len(queries)
        batch = [queries[(cursor + i) % len(queries)]
                 for i in range(min(per_run, len(queries)))]
        self.db.set_kv("search_cursor", str((cursor + len(batch)) % len(queries)))

        added = seen = kept = failed = 0
        for query in batch:
            try:
                r = self.find_leads(query)
            except Exception as e:
                failed += 1
                self.db.log(None, "auto_search_failed",
                            f"Search {query!r} failed: {explain(e, 300)}"[:400])
                continue
            added += r.get("added", 0)
            seen += r.get("seen", 0)
            kept += r.get("found", 0)

        waiting = len(self.db.leads_awaiting_approval())
        need_email = len(self.db.leads_needing_email())
        # Always say what happened. A run that finds nothing is the one the
        # user most needs explained, and it used to log nothing at all.
        summary = (f"Searched {len(batch)} of {len(queries)} areas, looked at "
                   f"{seen} businesses — {added} new leads.")
        if failed == len(batch):
            summary = (f"All {failed} searches failed — see the entries above "
                       "for why.")
        elif not added:
            if kept:
                summary += " Everyone without a website here is already in your list."
            elif seen:
                summary += (" Every business Google showed had a website. The "
                            "next run moves on to different areas.")
            else:
                summary += " Google returned nothing for these areas."
        self.db.log(None, "auto_search", summary)
        if added:
            self._notify(f"{added} new leads found",
                         f"{waiting} ready for your approval, {need_email} still "
                         "need an email address.", tags="mag")
        return {"ok": True, "added": added, "seen": seen, "kept": kept,
                "searched": len(batch), "total": len(queries), "failed": failed,
                "summary": summary}

    def research_missing_emails(self, force: bool = False, limit: int = None) -> dict:
        """Researcher: hunt public contact emails for leads that lack one.
        Results are SUGGESTIONS — a human accepts them before any outreach."""
        if not force and not self.config.get("auto_research_enabled"):
            return {"ok": True, "skipped": "researcher off"}
        if limit is None:
            limit = max(1, int(self.config.get("research_per_tick", 3) or 3))
        leads = self.db.leads_to_research(limit)
        found = 0
        for lead in leads:
            try:
                result = self._find_email(dict(lead))
            except Exception as e:
                self.db.log(lead["id"], "research_failed", explain(e, 300))
                self.db.update_lead(lead["id"], researched_at=_now())
                continue
            self.db.update_lead(lead["id"], researched_at=_now())
            if result.get("found"):
                self.db.update_lead(
                    lead["id"], suggested_email=result["email"],
                    suggested_email_source=(result.get("source") or "")[:300],
                    suggested_email_note=(result.get("note") or "")[:300])
                if self.config.get("auto_accept_emails", True):
                    # Straight onto the lead. The cold email to it is still
                    # read and approved by a person, with the address and
                    # where it came from both on screen.
                    self.accept_suggested_email(lead["id"])
                    self.db.log(lead["id"], "email_found",
                                f"Found {result['email']} for {lead['name']} "
                                f"({(result.get('source') or 'no source')[:120]}). "
                                "Check it on the Approve page before sending.")
                else:
                    self.db.log(lead["id"], "email_suggested",
                                f"Researcher found {result['email']} for "
                                f"{lead['name']} — needs your OK.")
                found += 1
            else:
                self.db.log(lead["id"], "email_not_found",
                            f"No email found online for {lead['name']}: "
                            f"{result.get('note', '')}"[:300])
        if found:
            self._notify(f"{found} email address{'' if found == 1 else 'es'} found",
                         "The Researcher turned up contact addresses — check "
                         "them on the Approve page.", tags="mag")
        return {"ok": True, "researched": len(leads), "found": found}

    def _find_email(self, lead: dict) -> dict:
        """An address for one lead, from whichever source can actually help.

        Hunter works from a domain, so it only has anything to say about the
        businesses whose site is dead or parked — for those it is far better
        than guessing. A business with no website at all has no domain, and
        goes to Claude's web search, which can read a Facebook page.
        """
        domain = _domain_of(lead.get("social_url"))
        if domain and (self.config.get("hunter_api_key") or "").strip():
            try:
                found = self.services.hunter_email(domain)
                if found.get("found"):
                    return found
            except Exception as e:
                self.db.log(lead.get("id"), "hunter_failed", explain(e, 200))
        return self.services.research_email(lead)

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
        """One background iteration: replies, payments, stuck transient stages,
        and scheduled lead discovery. Never sends cold outreach — that always
        waits for your approval."""
        self.process_replies()
        self.poll_payments()
        self.tick_transients()
        self.run_saved_searches()
        self.keep_stocked()
        self.research_missing_emails()

    def keep_stocked(self) -> dict:
        """Go and find leads before being asked, when the shelf runs low.

        Hunting costs Google calls, so this is deliberately lazy: only when
        there are few uncontacted leads left, only when a home town is set,
        and never more often than the interval. The meter above is the hard
        stop; this is the polite one.
        """
        cfg = self.config
        if not cfg.get("auto_search_enabled"):
            return {"skipped": "automatic hunting off"}
        area = (cfg.get("territory_base") or "").strip()
        if not area:
            return {"skipped": "no home town set"}

        floor = max(1, int(cfg.get("lead_floor", 15) or 15))
        waiting = len(self.db.leads_by_stage(STAGE_FOUND))
        if waiting >= floor:
            return {"skipped": f"{waiting} leads still waiting"}

        hours = max(1, int(cfg.get("hunt_interval_hours", 6) or 6))
        last = self.db.get_kv("last_hunt")
        if last and _age_minutes(last) < hours * 60:
            return {"skipped": "hunted recently"}
        self.db.set_kv("last_hunt", _now())
        return self.hunt(area, want=max(8, floor - waiting))

    def watch(self) -> list[dict]:
        """Look the whole app over and push for anything newly broken.

        Runs whether or not the pipeline is switched on — a paused app can
        still be misconfigured, and that is exactly when nobody is looking.
        A problem notifies once: it has to clear and come back to notify
        again, so a key you haven't got round to adding doesn't buzz all day.
        """
        found = checkup(self.db, self.config)
        problems = [f for f in found if f["level"] == FIX]
        try:
            known = set(json.loads(self.db.get_kv("watch_notified") or "[]"))
        except (TypeError, ValueError):
            known = set()
        ids = {f["id"] for f in problems}
        fresh = [f for f in problems if f["id"] not in known]
        if fresh:
            more = len(fresh) - 1
            self._notify(fresh[0]["title"],
                         fresh[0]["detail"]
                         + (f" (+{more} more)" if more else ""),
                         priority="high", tags="warning")
        if ids != known:
            self.db.set_kv("watch_notified", json.dumps(sorted(ids)))
        return found
