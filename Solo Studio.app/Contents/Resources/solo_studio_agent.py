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
from datetime import datetime, timezone

import requests

APP_NAME = "Solo Studio"


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
    "poll_interval_seconds": 120,
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
"""


class Database:
    """Thin sqlite wrapper. One connection per thread, WAL mode, atomic claims."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_data_dir(), "solo_studio.db")
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # -- leads ------------------------------------------------------------

    def add_lead(self, *, place_id, name, address, phone, category, email=None) -> int | None:
        """Insert a lead; returns new id, or None if this place already exists."""
        c = self._conn()
        try:
            with c:
                cur = c.execute(
                    "INSERT INTO leads (place_id, name, address, phone, category, email,"
                    " stage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (place_id, name, address, phone, category, email,
                     STAGE_FOUND, _now(), _now()),
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


class Services:
    def __init__(self, config: dict):
        self.config = config
        self._inkbox = None
        self._identity = None
        self._anthropic = None

    # -- Google Places (New) ----------------------------------------------

    def places_search_no_website(self, query: str, max_results: int = 60) -> list[dict]:
        """Text-search businesses and keep only those without a website."""
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
        results: list[dict] = []
        page_token = None
        while len(results) < max_results:
            body: dict = {"textQuery": query, "pageSize": 20}
            if page_token:
                body["pageToken"] = page_token
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code != 200:
                raise ServiceError(
                    f"Google Places error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            for p in data.get("places", []):
                if p.get("websiteUri"):
                    continue  # has a website — not our lead
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    continue
                results.append({
                    "place_id": p.get("id"),
                    "name": (p.get("displayName") or {}).get("text", "Unknown"),
                    "address": p.get("formattedAddress"),
                    "phone": p.get("nationalPhoneNumber"),
                    "category": p.get("primaryTypeDisplayName", {}).get("text")
                    if isinstance(p.get("primaryTypeDisplayName"), dict)
                    else p.get("primaryTypeDisplayName"),
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return results[:max_results]

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

    # -- lead discovery ----------------------------------------------------

    def find_leads(self, query: str) -> dict:
        found = self.services.places_search_no_website(query)
        added = 0
        for p in found:
            if not p.get("place_id"):
                continue
            if self.db.add_lead(**p) is not None:
                added += 1
        self.db.log(None, "find_leads",
                    f"Query {query!r}: {len(found)} no-website businesses, {added} new")
        return {"found": len(found), "added": added}

    # -- outreach ----------------------------------------------------------

    def send_outreach(self, lead_id: int) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["do_not_contact"]:
            return {"ok": False, "error": "Lead is flagged do-not-contact."}
        if not lead["email"]:
            return {"ok": False, "error": "Lead has no email address yet."}
        # Claim so a double-clicked button can't send two cold emails.
        if not self.db.claim(lead_id, [STAGE_FOUND], STAGE_CONTACTED):
            return {"ok": False, "error": "Lead is not in 'found' stage."}
        cfg = self.config
        fmt = {
            "lead_name": lead["name"],
            "your_name": cfg.get("your_name") or "the owner",
            "studio_name": cfg.get("studio_name") or "Solo Studio",
            "price": fmt_price(cfg.get("site_price_usd", 500)),
            "mailing_address": cfg.get("mailing_address") or "",
        }
        try:
            subject = cfg.get("outreach_subject", "").format(**fmt) or f"A website for {lead['name']}"
            body = cfg.get("outreach_body", "").format(**fmt)
            sent = self.services.email_send(to=lead["email"], subject=subject,
                                            body_text=body)
        except Exception as e:
            # Roll back so the lead can be retried.
            self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_FOUND)
            self.db.update_lead(lead_id, error=str(e)[:500])
            self.db.log(lead_id, "outreach_failed", str(e)[:500], needs_attention=True)
            return {"ok": False, "error": str(e)}
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
            self.db.log(None, "poll_error", f"Inbound email poll failed: {e}"[:500])
            return {"ok": False, "error": str(e)}
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
                        f"Couldn't classify reply ({e}) — handle manually.",
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
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_BUILDING_PREVIEW], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_BUILDING_PREVIEW)
                self.db.log(lead_id, "preview_failed",
                            f"Gave up building preview after {attempts} attempts: {e}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "preview_retry",
                            f"Preview step failed (attempt {attempts}): {e}"[:500])

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
                self.db.update_lead(lead_id, stripe_session_id=session_id,
                                    stripe_session_url=session_url)
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
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_SENDING_PAYMENT_LINK], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_SENDING_PAYMENT_LINK)
                self.db.log(lead_id, "payment_link_failed",
                            f"Gave up sending payment link after {attempts} attempts: {e}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "payment_link_retry",
                            f"Payment-link step failed (attempt {attempts}): {e}"[:500])

    def _resend_payment_link(self, lead_id: int, reply_rfc_id: str) -> None:
        """A lead replied after getting the link. Re-send the SAME link if the
        session is still open — never create a second one here."""
        lead = self.db.get_lead(lead_id)
        if lead is None or not lead["stripe_session_id"]:
            return
        try:
            session = self.services.stripe_get_session(lead["stripe_session_id"])
        except Exception as e:
            self.db.log(lead_id, "stripe_poll_error", str(e)[:500], needs_attention=True)
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
            self.db.log(lead_id, "email_failed", str(e)[:500], needs_attention=True)

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
                self.db.log(lead["id"], "stripe_poll_error", str(e)[:500])
                continue
            if session["payment_status"] == "paid":
                if self.db.claim(lead["id"], [STAGE_PAYMENT_LINK_SENT], STAGE_PAID):
                    self.db.update_lead(lead["id"], paid_at=_now(), attempts=0)
                    self.db.log(lead["id"], "payment_confirmed",
                                "Stripe confirmed payment. Deploying final site.")
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
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            # NEVER park a paid lead in 'error' silently — roll back to 'paid'
            # so delivery keeps retrying, and flag it loudly.
            self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_PAID)
            self.db.log(lead_id, "delivery_retry",
                        f"Final delivery failed (attempt {attempts}): {e} — "
                        "they HAVE paid; delivery will retry automatically.",
                        needs_attention=attempts == MAX_ATTEMPTS)

    # -- manual actions (dashboard buttons) --------------------------------

    def set_email(self, lead_id: int, email: str) -> dict:
        email = (email or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return {"ok": False, "error": "That doesn't look like an email address."}
        self.db.update_lead(lead_id, email=email)
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
        """One background iteration: replies, payments, stuck transient stages.
        Never initiates cold outreach — that is always a human action."""
        self.process_replies()
        self.poll_payments()
        self.tick_transients()
