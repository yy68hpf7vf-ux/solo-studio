# Call First

Find local businesses, ring them, and let the software do everything after
"yes".

This is a rebuild of Solo Studio with the order reversed. It exists because the
original failed, and it is worth being exact about how, because the fix is the
whole design.

## Why the last one didn't work

Solo Studio found businesses **with no website** — the strongest possible pitch
— and then tried to reach them by cold email.

It could not. A business with no website has no email address anywhere online
either: there is no site to read one off, no domain for a lookup service, and
map listings almost never carry one. The only route left was a paid AI web
search at about 3.5¢ a head, which needed credit that ran out, and which mostly
came back empty anyway.

So the queue sat at zero. 1,186 businesses found, 0 reachable, and a screen
that said "the Researcher hunts for them online" over a list that was never
going to move.

## What changed

**You get the email address on the phone.**

That one move fixes everything the old app couldn't:

| | Solo Studio | Call First |
|---|---|---|
| How you reach them | cold email | you ring them |
| Where the address comes from | scraped, guessed, or bought | they read it to you |
| Cost to find a prospect | paid API lookups | free |
| Cold-email exposure (CAN-SPAM) | yours, at volume | none — there is no cold email |
| Works with $0 of AI credit | no | yes, right up to the sale |
| Who can be reached | the ones with a website | anyone with a phone |

A business that has just spoken to you also *opens* your email, which no cold
send ever achieves.

## How it runs

1. **It finds businesses** near your area and puts them on the call sheet with
   a phone number, their current website, and a link to their listing.
2. **You call them.** The screen gives you the number as one big tap-to-dial
   button and the words to open with underneath.
3. **They say yes and give you an email.** You type it in and press one button.
4. **Everything after that is automatic:** Claude designs the site, it deploys
   as a watermarked preview, the preview is emailed, a payment link follows,
   and the clean site goes live once Stripe confirms the money.

Steps 1 and 4 need no attention. Step 2 is the job.

## The one rule that never bends

**No site is ever delivered unpaid.** What a prospect sees before paying is
always watermarked, and Stripe is asked twice — once when the payment lands and
again at the moment of delivery. If Stripe stops saying "paid" between those two
checks, delivery stops and the lead goes back to awaiting payment.

There is also no way to email a stranger. `send_outreach`, `render_outreach` and
the whole email-research path are gone, and a test fails if any of them comes
back.

## What it costs

| | Cost |
|---|---|
| Finding businesses (Google Places) | **free** — 5,000 lookups/month, and the app refuses past 4,500 |
| Finding businesses (OpenStreetMap) | **free**, no key at all |
| Checking their website | free |
| Calling them | your phone |
| Designing a site | ~$1 of Claude credit — **only after somebody says yes** |
| Hosting the site (Netlify) | free tier |
| Taking payment (Stripe) | their usual percentage |

One person can call perhaps 40 businesses a day, about 1,000 a month. One
Google lookup returns up to 20 businesses, so the free allowance is several
times more than this app can physically use. **Prospecting cannot cost you
anything**, and Claude is never asked to design anything on spec.

If an area is exhausted — a small town with one trade may only ever yield
twenty businesses — the app notices that a search added nothing and rests for
six hours instead of searching the same place every minute. That silent
repeating spend is precisely what went wrong last time.

## Setting it up

Everything is on the Setup page; no files to edit.

1. **Your area** (e.g. `Napanoch, NY`) and optionally which trades to look for.
   Leave trades blank for a sensible built-in list.
2. **Google Places API key** — this is all you need to start calling.
3. The other three keys (**Anthropic**, **Netlify**, **Inkbox**, **Stripe**) are
   only touched once someone agrees, so you can fill them in later.

Then:

```
python app.py
```

It opens at <http://127.0.0.1:8748/>.

## Before you sell to a real person

Do one practice run end to end with your own email address and Stripe in test
mode (card `4242 4242 4242 4242`). Ring yourself, type your own address, watch
the preview arrive, pay the test link, and confirm the watermark comes off. It
takes ten minutes and it is the only way to know the whole chain works before
somebody's actual money is involved.

## Tests

```
python -m unittest discover -s tests -t .
```

37 tests. The ones that matter most are `NothingGoesOutUnpaidTest` (the payment
gate), `NoColdEmailTest` (that the old front end cannot come back), and
`WhoGoesOnTheSheetTest` (that a business with a working website is still worth
ringing, and one with no phone number is not a lead).

## Layout

- `engine.py` — everything that isn't the screen: the database, the services
  (Google, OpenStreetMap, Claude, Netlify, Stripe, email), and the pipeline from
  "yes" to a delivered site. Derived from Solo Studio's agent with the crawler,
  the email researcher and the cold-email path removed — 2,900 lines, down from
  3,900.
- `app.py` — the dashboard. Four pages: the call sheet, what's in progress,
  activity, and setup.
- `tests/` — the suite above.
