# Solo Studio

An automated lead-gen + sales pipeline for a one-person web design studio.

Dark by design — indigo ground, hot pink accent, soft glass panels and
rounded corners. Nothing moves on its own and nothing moves under the pointer:
there was a drifting light show and a tilt-on-hover here for a while, and both
were taken out again.

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

**Four places he looks.** Google's text search ranks by prominence, which is
almost a definition of "has a website" — so it is now the least of what he uses.

| Source | What it adds | Cost |
|---|---|---|
| **Google, by distance** | The businesses *nearest* a point rather than the best known — the one-van operation on the side street, which is the actual market. | Your existing key, metered |
| **OpenStreetMap** | A completely separate map of the world. No key, no limits worth worrying about, and full of small businesses with a phone number and no website. | Free |
| **Yelp** *(optional)* | A fourth index, for coverage the others miss. | Free tier |
| **Google, by text** | "Plumbers in Ellenville" — still useful in small towns. | Your existing key, metered |

Each source fails on its own: OpenStreetMap being busy doesn't stop Google, and
Google being down doesn't stop OpenStreetMap. One honest caveat — Yelp's search
returns a business's *Yelp page*, never its own website, so those leads are
marked as a directory page only and are a weaker signal than the rest, where
the real site got checked.

**The live map.** The **Map** page draws the United States — real state
outlines from public-domain Census boundary data — with **all 1,321 cities on
the route** already plotted, so it shows where he's going as well as where he's
been. Faint dots are still to come; swept ones grow with the leads found there;
the city he's on right now pings. It refreshes itself every few seconds, and the
coastline and the dots share one projection, so a city can't land in the wrong
state.

Those coordinates are built into the app, which also means the crawl no longer
spends a Google lookup per city just to learn where it is — about 1,300 calls
saved. The handful not in the list are still looked up when he reaches them.

**Filtering what you look at.** The Approve page splits the queue into the ones
ready to send and the ones still needing an address, with counts. It narrows
what's on screen and never changes what's collected: filtering hides, it never
deletes.

**What the email lookup costs, and why it used to hurt.** Asking a model with
web search runs about $10 per 1,000 searches on top of tokens; on the big model
with five searches a lead that is roughly 20 cents each, which is $200 across a
thousand leads. It now goes cheapest-first:

| | Cost |
|---|---|
| OpenStreetMap already had the email | free |
| Read it off the page we already have the link to | free |
| **Hunter** *(optional)*, when there's a domain | pennies |
| A model with web search — small model, two searches, capped | ~3.5¢ |

That last one is capped at 200 paid lookups a month, so the worst the app can
spend on addresses is about $7. The free routes carry on after the cap, and
Setup shows what has actually been used — lookups and Google calls both — so
it is a number, not a guess. Set the cap to 0 to never spend Claude credit on
addresses at all.

**One dial for Claude credit,** on Setup. Finding leads spends none of it at any
setting — that part is Google and OpenStreetMap.

| Setting | Address lookups | Ceiling |
|---|---|---|
| **Off** | none — free routes only | **$0** |
| **Frugal** *(default)* | cheap model, 10 a day | **$1.75/month** |
| **Normal** | best model for replies, 25 a day | **$7/month** |

Everything else is bounded by something real happening: reading a reply that
actually arrived (capped at 200 tokens, since the answer is one word),
answering you on the Ask page, designing a site for someone who has asked for
one. Nothing runs in a loop.

Designing a site is deliberately *not* on the dial — it always uses the best
model your account can reach. It only runs once somebody has said yes, it costs
about a dollar, and it's the thing you're selling for $500.

So on the shipped setting, the app cannot spend more than about **$1.75 a month**
unless a sale is involved.

**Finding the email.** Two ways, picked automatically. Where the business has a
domain — a dead or parked site — **Hunter** *(optional)* looks up addresses on
it, which beats guessing. Where there is no website at all there is no domain
to look up, so Claude's web search reads their Facebook page or directory
listing instead.

**He crawls the country on his own — city by city, state by state.** No typing,
no buttons. Every city in the United States is queued: your own state first,
biggest cities first within each state, and the towns immediately around home
before any of it. He places each city on the map once, sweeps a grid of spots
across it, and moves to the next.

The grid matters: Google returns the twenty businesses nearest a point and
nothing more, so one point per city would be one street corner per city.

He doesn't stop. There's no lead ceiling any more — stopping at a round number
just meant stopping — and progress is saved, so closing the app and opening it
tomorrow carries on from the same city.

What he does do is **pace himself**. Google gives 5,000 searches a month free;
at a few spots a minute the whole allowance would go in a day and then he'd sit
still for four weeks, which reads exactly like the app being broken. So the
budget is spread across the month, with a burst at the start so a fresh install
finds something in the first few minutes. He is always working, and never
charges you.

**No home town needed.** If you haven't set one he takes it from the postal
address you already gave for the emails. If there's no address either, he
crawls the country anyway rather than doing nothing. "Without clicking
anything" means exactly that.

**It is on out of the box.** Autopilot and lead hunting used to ship switched
off, which meant a new app sat there doing nothing and saying nothing about it.
Both are on now, and upgrading turns them on once for anyone who had them off
by default — after which a deliberate "off" sticks.

**He never goes quiet.** Every reason JARVIS might not be working is a fault at
the top of the Dashboard, not something to guess at: switched off, hunting
switched off, the month's search budget used up, a key missing, no credit. If
nothing is happening, the screen says why.

**It works with nothing else working.** Out of Claude credit, OpenStreetMap
down, Yelp not set up — the crawl still runs, because the city list is built in
rather than looked up.

**Only businesses.** A map search returns everything on the map — sheriff's
offices, schools, churches, the city water department. None of them buy
websites, so they're dropped before they reach the queue, on every source.
The category is trusted; a name is not, because "Church Street Auto Repair"
and "Courthouse Coffee" are real businesses and a blunt word match throws them
away. Losing a real lead costs more than letting one town hall through.

**What's gone out.** The Dashboard says how many cold emails were sent today,
against the daily cap, how many all told, and how many are waiting for you.
Nothing leaves without your approval, so that line is a record of your own
decisions.

**You can see whether he's running.** The Dashboard opens with a line saying
so — a green dot and "JARVIS is working — checked in 14s ago", or a red one and
"JARVIS has stopped", plus which version is actually running. The background
worker writes a heartbeat every round, so a stopped app looks different from a
working one instead of identical. If it ever says stopped, quit and reopen.

**He starts on launch.** Opening the app puts him to work: check what needs
you, crawl if there's map left, look up missing addresses.

**JARVIS opens the websites.** Google only tells you whether a business has a
link. Plenty of those links are dead, or a Facebook page. So he fetches each
one and looks — and keeps the business only if it has **no website of its
own**. Checking costs nothing: ordinary web requests, not billable searches.

One bar, not three. A dead link, a parked domain or a dated site all mean the
business *has* a website; that's a different conversation and a weaker one. A
Facebook or Yelp page counts as no website, because it is one: there's nowhere
of their own to send a customer, and "you haven't got a website" is true, easy
to say and impossible to argue with.

**What counts as "no website".** A business whose only link is a Facebook or
Instagram page counts — it has nowhere of its own to send a customer, which is
the whole pitch, and someone there already tried, which makes it a warmer lead
than a blank listing. The approve screen shows which is which.

**Just type a town.** Put "Los Angeles, CA" in the box and JARVIS goes hunting:
he works out the towns around it, tries a different trade in each, and stops as
soon as he has enough — so a good area costs two or three searches, not twelve.
Name a trade as well ("plumbers in Riverside, CA") and he runs that first, then
goes hunting anyway if it turns up nothing. He runs in the background and the
page updates itself when he's back.

This exists because searching a business directory for a city name returns the
city and its biggest firms, every one of which has a website — an empty result
that looks like a broken app. It isn't; it's the wrong search, and the app
should be the one that knows that.

**Where the leads are.** Not in the city centre. Type your city into the
territory builder and it works outwards to the small towns and suburbs around
it, smallest first, because in a big city every business already has a site.
Every search now reports what it looked at and why it kept or skipped it, so a
run that finds nothing tells you which of those it was.

Google Places is last on purpose: it's the only one that needs a card on file,
and everything else works without it — you'd just be adding businesses by hand.

Anthropic is pay-as-you-go and a brand-new account starts at zero, so add
credit under **Plans & Billing** at console.anthropic.com before you use it —
$5 goes a long way, since a whole website costs cents. Until you do, every
Claude step fails with "credit balance is too low", which the app now says in
plain English along with where to fix it.

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
2. Click **Restart Solo Studio** on the button that appears. It's back in a few
   seconds — that restart is what opens it to your Wi-Fi.
3. A **QR code** appears on the Setup page. Point your phone's camera at it and
   tap the link, then enter your PIN.
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

The Restart button brings the app back itself, so it works however Solo Studio
was started — from the icon, from an app bundle too old to know about the
button, or by hand from a terminal. If nothing on disk will start, it refuses
to restart at all and stays on the copy already running rather than leaving you
with no app.

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

## JARVIS does the work

He runs continuously in the background, and none of it happens inside a page
request — hunting an area or looking up a batch of addresses takes a minute or
two, and a page that hangs that long reads as a broken app. Whatever he is
doing shows as a banner on every page, and the page comes back on its own when
he's done.

On each round he reads replies, checks payments, unsticks anything mid-step,
runs your saved searches, **restocks when the shelf runs low** (fewer than 15
leads waiting, at most once every 6 hours, and only if you've set a home town),
and **looks up the email addresses** of leads that don't have one.

An address he finds goes straight onto the lead, with a note of where he found
it. The Approve page shows the address, marks it "JARVIS found this, you
haven't checked it", and links the page he took it from. **Nothing about
sending changes:** a person still reads and approves every cold email, with
that address in front of them. Prefer the old second-click? Turn off
auto-accept in Setup and he'll only suggest.

**The money tap.** Google gives 5,000 searches a month free and charges about
$32 per thousand after that. Every call — a hunt, a saved search, a button —
counts against one monthly meter, and JARVIS refuses to make another once it
hits the cap, which ships at 4,500, under the free line. Letting him work
continuously is only safe because something says no on your behalf.

## JARVIS keeps watch

The app checks itself continuously — on every background tick, whether or not
autopilot is on, because a paused app can still be misconfigured and that is
exactly when nobody is looking. What it finds is sorted worst first and shown
in three places: a panel at the top of the Dashboard, the right-hand column of
the JARVIS screen, and the brief the Ask helper reads, so asking "what's wrong?"
gets the same answer that's on screen.

Three levels:

| | What it means |
|---|---|
| **Fix** | Broken. A missing key, no API credit, a lead that gave up, a step failing on repeat, phone access with no PIN, no mailing address (cold email needs one by law). |
| **Waiting** | Working, but it needs you: emails to approve, leads with no address found yet. |
| **Watch** | Worth knowing. Autopilot off, Stripe in test mode, a payment link nobody used for a week, a preview nobody answered. |

Anything at **Fix** level pushes a notification to your phone once, when it
first appears. It has to clear and come back to notify again, so a key you
haven't got round to adding doesn't buzz all day. Waiting and Watch never push.

The watchman only ever reports. It cannot send an email, move money, or change
a lead's stage — there is a test that fails if that ever changes.

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

### The call list — the one channel besides email

Google Places gives you a **phone number** even when there's no email, so a
lead that outreach can't reach is not a dead end. The **Calls** page lines those
up: phone-only businesses first, whoever you haven't tried at the top, a
tap-to-call button, and something to say when they answer. Take the email off
them on the phone, type it in, and they drop into the normal Approve queue —
the cold email still needs your OK before it sends.

**Why the app will never dial or text on its own.** In the US the TCPA follows
the phone *number*, not the context, and most small business numbers are
mobiles — automated cold texts run $500–$1,500 **per message**, and the FCC has
ruled that AI-generated voices count as "artificial" calls under the same rules,
at the same price. Carriers block unregistered bulk sending anyway. So there is
no send button here on purpose: the app finds them and hands you a `tel:` link.
You calling is ordinary business, costs nothing, and for trades it converts
better than email ever will.

### A line a day

The top of the Dashboard carries one line, the same all day, a different one
tomorrow. It cycles the whole list before anything comes round again. Most of
them are written for this particular job — one person, cold email, a lot of
silence between the yeses — rather than pulled from a quotes site; the few that
are quoted are proverbs or name a source that can be pointed at. No API is
involved, so it works on a brand-new install before any keys are in.

### The studio — the house the work moves through

Open **Studio** to see the whole business as a cutaway house. Each specialist
has a room, and every lead you have is a dot standing in the room it's actually
in: Scout, Researcher and Copywriter on the top floor finding and preparing;
**your desk on the landing**, which nothing gets past without you tapping
approve; Triage, Designer and Deployer below that; Biller and Delivery on the
ground floor; and the vault at the bottom with what you've collected.

It's live. Rooms with a missing key sit dark and say which key. Rooms on duty
breathe. **Tap any room** to see exactly which businesses are in it. And when a
deal actually moves — a reply lands, a payment clears — you see the light
travel from one room to the next.

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

- **Automatic lead hunting:** you shouldn't have to know every town in your
  county. On Setup, put in **your town** and **how far you'd travel**, check the
  list of trades, and hit **Build my search list** — it looks up the real towns
  around you and writes a search for every trade in every one of them. Then tick
  *Search for new leads automatically*.
- **What that costs:** Google bills per search — about $32 per 1,000 calls,
  with the first 5,000 a month free. A run only spends the budget you set
  (*searches per run*) and picks up where it left off next time, so a list of
  240 searches works round over a couple of weeks instead of running all of it
  twice a day. Setup shows the projected monthly figure and warns you if a
  setting would take you past the free allowance.
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
- `tests/test_house.py` — the Studio: every lead in exactly one room, rooms
  moving when a deal moves, and the landing counting what's waiting on you.
- `tests/test_calls.py` — the call list, including a test that no route
  anywhere in the app can place a call or send a text.
- `tests/test_territory.py` — building the search list from a town and a
  radius, and the per-run budget that keeps Google's bill at zero.

```bash
pip install flask requests anthropic inkbox
python3 -m unittest discover -s tests -v   # run the test suite
python3 dashboard_app.py                   # run the dashboard locally
```

`SOLO_STUDIO_HOME=<dir>` overrides where config/database live (tests use it).
