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
- Cold emails only ever go out after **you approve them** on the Approve page,
  where you see the exact wording first — the background loop finds leads and
  handles replies and payments, but never sends outreach on its own.
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
   then your browser opens the dashboard automatically.

**One-time requirement: Python 3.13.** The Python that comes with macOS is
too old (3.9), and the newest one (3.14) is too new for some of the app's
components. If the app tells you Python is missing or wrong, it opens the
right installer for you automatically — run it, then open Solo Studio again.
(Direct link: [Python 3.13.7 for macOS](https://www.python.org/ftp/python/3.13.7/python-3.13.7-macos11.pkg).)

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

## Putting it on your phone

Solo Studio installs on your phone as a home-screen app — no App Store needed.
The Mac app is the engine, so it must be open for the phone to see anything.

1. On the Mac, open **Setup** → tick **"Let my phone open this dashboard"**,
   set a 4–8 digit PIN, click **Save settings**.
2. **Quit and reopen Solo Studio** (this is what switches on phone access).
3. Back on the Setup page, a **QR code** appears. Point your phone's camera at
   it and tap the link, then enter your PIN.
4. Tap **Share → Add to Home Screen**. You now have a Solo Studio icon.

**Notifications (works anywhere, not just at home):** tick **"Push
notifications to my phone"** on Setup and Save. Install the free **ntfy** app,
tap **+**, and subscribe to the topic name Setup shows you. Your phone then
buzzes for replies, previews, payment links, payments, and deliveries. Use
**"Send a test notification"** to confirm it works.

## Running it 24/7 in the cloud (optional)

By default Solo Studio runs on your Mac, which means it only works while your
Mac is on. To have it run around the clock — replying to leads and delivering
paid sites at 3am while your laptop is shut — deploy it to a small server.
Cost is about **$7/month**. You do this once, in a browser.

1. Make a free account at [render.com](https://render.com) and connect your
   GitHub.
2. Click **New → Blueprint**, pick this repository. Render reads
   `render.yaml` and fills in everything for you.
3. It asks for one value: **SOLO_STUDIO_PASSWORD**. Invent a strong password
   (10+ characters) — this is what stops strangers reaching your dashboard.
   Save it in your password manager.
4. Click **Apply** and wait a few minutes. You get a web address like
   `https://solo-studio-xxxx.onrender.com`.
5. Open that address, sign in with your password, and fill in the **Setup**
   page with your API keys exactly as you would on the Mac. Turn on
   **autopilot**.

That's it — the pipeline now runs on the server. Open the same web address on
your phone and tap **Share → Add to Home Screen** for an app icon that works
from anywhere, on any network.

**Notes worth knowing**

- Your API keys live on the server, entered through Setup over HTTPS. The
  site is password-protected, sign-in attempts are rate-limited, and a
  password shorter than 10 characters locks everyone out rather than running
  insecurely.
- The paid plan matters: the **persistent disk** in `render.yaml` is what
  keeps your leads database safe across restarts. A free instance would lose
  it and also fall asleep.
- Keep exactly **one** copy running (cloud *or* Mac, not both against the same
  mailbox), so two pipelines don't answer the same lead.
- Any host that runs a Python web app with a persistent disk works the same
  way; Render is just the least fiddly.

## Day-to-day use

**What runs by itself, and what needs you:**

| Step | Who does it |
|---|---|
| Finding businesses with no website | **Automatic** (saved searches on a schedule) |
| Getting their email address | **You** — Google doesn't publish it |
| Sending the cold email | **You approve it**, then it sends |
| Reading replies, judging interest | Automatic |
| Designing + deploying the preview site | Automatic |
| Sending the payment link | Automatic |
| Watching Stripe for payment | Automatic |
| Delivering the paid, watermark-free site | Automatic |

So the loop is: **find → you add an email → you approve → the machine closes
the deal.**

- **Automatic lead hunting:** on the Setup page, list the searches you want
  (one per line, e.g. `plumbers in Riverside, CA`) and tick *Search for new
  leads automatically*. New no-website businesses are added on a schedule and
  your phone buzzes to tell you.
- **The Approve page:** every found business waits here. You see the exact
  email that would go out, word for word, and tap **Approve & send** (or
  *Approve & send all*). Nothing is ever emailed without that tap. Businesses
  still missing an email address are listed underneath with a box to paste
  one in — that's the one job only you can do.
- **Daily cap:** Setup limits how many cold emails go out per day (20 by
  default). A brand-new mailbox that blasts hundreds a day gets flagged as
  spam, which kills your delivery rate. Slow beats blocked.
- **Live screens:** every page updates itself — JARVIS refreshes every few
  seconds, and the Dashboard/Approve/Activity pages notice new leads, replies
  and payments within about six seconds and refresh on their own. If you're
  part-way through typing, they don't yank the page out from under you: a
  "New activity — tap to refresh" pill appears instead.
- **Autopilot:** with it on, the app checks replies and payments every minute
  and drives every conversation to done. Anything ambiguous
  (questions, change requests, unknown senders) is parked in **Needs your
  attention** instead of guessing.

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
