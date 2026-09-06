# Solo Studio

An automated lead-gen + sales pipeline for a one-person web design studio.

Dark by design — deep black, soft glass panels and rounded corners, to sit
comfortably next to the rest of your Mac.

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

Open **Setup** in the dashboard. It walks you through the five keys one card at
a time — each says what it's for, how long it takes, the exact clicks, and has a
button that opens the right page. A banner at the top counts how many you have
left. Work down the list in order:

| # | Key | Time | What it does |
|---|---|---|---|
| 1 | Anthropic (Claude) | 2 min | Writes replies, designs each site |
| 2 | Inkbox | 3 min | The mailbox that sends and receives |
| 3 | Netlify | 2 min | Puts each site online |
| 4 | Stripe | 3 min | Takes the payment |
| 5 | Google Places | 10 min | Finds businesses with no website |

Google Places is last on purpose: it's the only one that needs a card on file,
and everything else works without it — you'd just be adding businesses by hand.

Then fill in your name, studio name, **mailing address** and price, click
**Save settings**, and click **Test connections** — every row should show a
green check before you start.

**Practise before you go live.** Start with a **Stripe TEST key**
(`sk_test_…`), add a lead pointing at your own email address, and run the whole
pipeline through with the test card `4242 4242 4242 4242` (any future expiry,
any CVC). Switch to your live key (`sk_live_…`) only once you've watched a fake
sale go through end to end.

Anything you shouldn't normally touch — the Claude model name, how often the
app checks for replies, the Inkbox handle — is tucked into **Advanced** at the
bottom of the page, already set sensibly.

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

## Updating

You never have to re-download. Open the **Updates** page in the app:

1. It tells you whether a new version exists and what changed.
2. Click **Install update**, then **Restart Solo Studio**.
3. The page goes blank for a few seconds and comes back on the new version.

Your leads, settings and API keys are never touched — they live outside the
app's code. Updated code is written to
`~/Library/Application Support/Solo Studio/app/`, never into the .app bundle,
so macOS permissions and code signing stay intact. A download that isn't valid
Python is rejected before anything is overwritten, and if an installed update
somehow won't start, the launcher falls back to the version inside the app
bundle and tells you.

**On the cloud version there's nothing to do at all** — your host redeploys
automatically whenever the code changes.

## Running it 24/7 in the cloud (optional)

By default Solo Studio runs on your Mac, which means it only works while your
Mac is on. To have it run around the clock — replying to leads and delivering
paid sites at 3am while your laptop is shut — deploy it to a small server.
Cost is about **$7/month**. You do this once, in a browser.

**One-click start:**
[Deploy to Render](https://render.com/deploy?repo=https://github.com/yy68hpf7vf-ux/solo-studio)

1. That link asks you to sign in to Render (free account) and connect GitHub.
2. Render reads `render.yaml` and fills in everything itself — runtime, start
   command, storage disk, health check.
3. It asks you for one value: **SOLO_STUDIO_PASSWORD**. Invent a strong
   password (10+ characters) — this is what stops strangers reaching your
   dashboard. Save it in your password manager.
4. Confirm the plan (Starter, ~$7/month — needed for the storage disk) and
   click **Apply**. Wait a few minutes for the first build.
5. You get a web address like `https://solo-studio-xxxx.onrender.com`. Open
   it, sign in with your password, and fill in the **Setup** page with your
   API keys right there in the browser. Turn on **autopilot**.

You never have to touch the Mac app again if you don't want to — the live
site is the whole thing, and your phone can install it from that same
address.

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
| Getting their email address | **Researcher suggests, you accept** |
| Sending the cold email | **You approve it**, then it sends |
| Reading replies, judging interest | Automatic |
| Designing + deploying the preview site | Automatic |
| Sending the payment link | Automatic |
| Watching Stripe for payment | Automatic |
| Delivering the paid, watermark-free site | Automatic |

So the loop is: **find → you add an email → you approve → the machine closes
the deal.**

### Ask — the helper built into the app

The **Ask** page is a chat with Claude that already knows your business. It sees
your live pipeline every time you ask something: which leads are waiting for
approval, what's in flight, what's errored, what needs your attention, your
revenue, and which API keys are still missing. So "what should I do next?" gets
a real answer about your actual leads, not a generic one.

It's **advisory only, by design.** It cannot send an email, approve a lead,
create a payment link, deploy a site, move money, or change a setting — no tools
are wired up to it, so it structurally cannot act however you phrase the
request. It tells you which page and which button instead. The payment gate is
described to it as something never to work around.

**It's on JARVIS too.** There's an `▸ ASK JARVIS ANYTHING…` bar along the
bottom of the JARVIS screen — start typing anywhere on the HUD and it takes the
keystrokes. Answers print into a terminal-style console over the HUD; **Esc**
or **Close** puts you back. It's the same conversation as the Ask page, so you
can start a question on one screen and carry on from the other.

Your chat is saved in the app's database, so it's still there tomorrow and on
your phone. **Clear chat** wipes it. Answers use your own Anthropic key — the
same one that designs the sites — so they cost a fraction of a cent each.

### The team

Open the **Team** page to see all eight specialists, what each has done, and
whether it's on duty or waiting on a key:

| | Specialist | Job | Runs on |
|---|---|---|---|
| 🔭 | **Scout** | Finds local businesses with no website | Google Places |
| 🕵️ | **Researcher** | Hunts their contact email online — *suggests only* | Claude + web search |
| ✍️ | **Copywriter** | Writes each cold email — *never sends without your OK* | Your template |
| 📬 | **Triage** | Reads replies, judges interest | Claude |
| 🎨 | **Designer** | Designs the one-page site | Claude |
| 🚀 | **Deployer** | Publishes the watermarked preview | Netlify |
| 💳 | **Biller** | Sends the payment link, watches for payment | Stripe |
| 📦 | **Delivery** | Ships the clean site once paid — *gated on Stripe* | Netlify + Stripe |

Only Triage, Designer and Researcher use AI judgment. Payments, deploys and the
payment gate are plain deterministic code on purpose — those must be exactly
right every time, and an LLM adds risk there with no upside.

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
- **Ask:** stuck on anything — a key, a lead, what to do next — ask the app
  itself on the **Ask** page instead of going somewhere else. It can see your
  pipeline; it can't press buttons for you.
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
- `tests/test_assistant.py` — the Ask helper: what it can see, what it stores,
  and that it is never handed tools it could act with.

```bash
pip install flask requests anthropic inkbox
python3 -m unittest discover -s tests -v   # run the test suite
python3 dashboard_app.py                   # run the dashboard locally
```

`SOLO_STUDIO_HOME=<dir>` overrides where config/database live (tests use it).
