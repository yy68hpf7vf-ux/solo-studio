# Solo Studio

An automated lead-gen + sales pipeline for a one-person web design studio.

**What it does, end to end:**

1. **Finds local businesses with no website** (Google Places API).
2. **Cold-emails them** (Inkbox API). Email only — Inkbox blocks cold SMS by
   design, so texting is not part of the pipeline.
3. When a lead **replies interested**, Claude designs a one-page site and a
   **watermarked preview** is deployed to Netlify; the link is emailed back.
4. When they reply that they **like it**, a **Stripe Checkout payment link**
   is emailed.
5. The app **polls Stripe in the background**; the moment payment clears, the
   **clean final site** (watermark removed) replaces the preview at the same
   address and the lead gets a "your site is live" email.

**Hard safety rules built into the code:**

- The final site is *never* deployed unless Stripe itself confirms the payment
  as paid — there is no button or code path that skips the payment gate.
- Every pipeline step is guarded so duplicate replies, double-clicks, or
  overlapping background checks can't send double emails, create a second
  payment link, or deliver twice.
- Cold emails only ever go out when **you** click "Send cold email" — the
  background autopilot handles replies and payments, never new outreach.
- Anyone who replies "unsubscribe" is flagged and never emailed again, and the
  cold-email template includes your mailing address and an opt-out line
  (both required by US law for commercial email — see "Legal" below).

---

## Installing on your Mac

1. On this repository's GitHub page, click the green **Code** button →
   **Download ZIP**.
2. Double-click the downloaded ZIP to unpack it.
3. Drag **Solo Studio.app** into your **Applications** folder.
4. First open only: **right-click** the app → **Open**. If your Mac says the
   app can't be checked/verified, open **System Settings → Privacy &
   Security**, scroll down, and click **"Open Anyway"** next to Solo Studio,
   then try again. (This appears because the app isn't signed with an Apple
   developer certificate.)
5. The very first launch spends about a minute installing its components,
   then your browser opens the dashboard automatically. If nothing happens,
   Python 3 may be missing — the app will point you to python.org to install
   it (one time).

To keep it in your Dock: while the app is running, right-click its Dock icon
→ **Options → Keep in Dock**.

## First-time setup

1. In the dashboard, open **Setup** and paste in your API keys:
   Google Places, Inkbox, Anthropic (Claude), Netlify, and Stripe.
2. Fill in your name, studio name, **mailing address**, and price.
3. Click **Save settings**, then **Test connections** — every row should show
   a green check before you start.
4. Start with a **Stripe TEST key** (`sk_test_…`) and send the pipeline
   through a lead pointing at your own email address. Switch to your live key
   (`sk_live_…`) only when the whole flow has worked for you once.

Everything is stored on your Mac in
`~/Library/Application Support/Solo Studio/` (`config.json` + a small
database). No keys ever leave your machine except to call the services they
belong to.

## Day-to-day use

- **Find new leads:** type a search like `plumbers in Riverside, CA`.
  Only businesses *without* a website are kept.
- **Add emails:** Google doesn't publish business email addresses, so each
  new lead has an empty email box — look the address up (Yelp, Facebook, a
  quick call) and save it. Outreach can't go out without it.
- **Send cold email:** per-lead button, with a confirmation. Always manual.
- **Autopilot:** turn it on and the app checks for replies and payments every
  couple of minutes *while the app is open*, moving leads through preview →
  payment link → delivery automatically. Anything ambiguous (questions,
  change requests, unknown senders) is parked in **Needs your attention**
  instead of guessing.

## Legal (worth 60 seconds)

Cold-emailing US businesses is legal under CAN-SPAM but has requirements the
app helps you meet: a truthful sender, your **physical mailing address** in
every message (that's why Setup asks for it), and honoring opt-outs (handled
automatically — "unsubscribe" replies are flagged do-not-contact forever).
If you email into other countries, check local rules (e.g. Canada's CASL and
the EU's ePrivacy rules are much stricter).

---

## For developers

- `solo_studio_agent.py` — all core logic: config, SQLite state machine,
  Google Places / Inkbox / Claude / Netlify / Stripe clients.
- `dashboard_app.py` — Flask dashboard (binds to 127.0.0.1:8747).
- `Solo Studio.app/` — macOS bundle; its `Resources/` holds a copy of the two
  Python files above (keep them in sync when editing).
- `tests/test_pipeline.py` — state-machine stress tests against fake
  services: payment gating, double-charge/double-preview guards, crash
  resume, concurrency races.

```bash
pip install flask requests anthropic inkbox
python3 -m unittest discover -s tests -v   # run the test suite
python3 dashboard_app.py                   # run the dashboard locally
```

`SOLO_STUDIO_HOME=<dir>` overrides where config/database live (tests use it).
