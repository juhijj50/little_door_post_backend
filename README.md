# The Little Door Post — API

FastAPI + Neon (Postgres). Stores sign-ups and, once Razorpay is approved,
takes the payment for them.

## Run

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # then paste your Neon connection string
python -m app.migrate           # creates the tables
uvicorn app.main:app --reload --port 8000
```

Interactive docs while it runs: <http://localhost:8000/docs>

## The monthly window

A window runs from the **6th of one month to the 5th of the next**, and fills
that next month's edition. Pay on 6 October and you are buying the November
letter. Windows run back to back — one shuts, the next opens the following day
— so there is never a stretch when the club cannot be joined.

`OPEN_DAY` and `CLOSE_DAY` in `app/cycles.py` are the rule; the `cycles` table
is the exception, and whatever a row says wins for that month.

**Counting a month is on its own clock.** `DISPATCHED_AFTER` (10 days past the
close) is when a month's envelopes are counted against everyone's remaining
total. It used to key off the next window opening, which worked only while that
was the 15th. With windows opening on the 6th that test would fire the day
after a window shut — before a single envelope had been posted — and expire
every one-month reader on the way out of the door.

## Where the post goes

`app/countries.py` is the rule, and `/api/config` serves the same list to the
form, so the dropdown cannot offer somewhere an order would be refused for.

The EU, the EEA and the UK are **excluded on purpose**: consumer law there
gives a distance buyer fourteen days to withdraw and be refunded in full,
including the envelope already posted. That right follows the buyer, so it
would bind this business the moment it sold to Dublin. `refusal()` says so in
words a reader can act on, and aliases mean somebody typing "England" is told
we do not post there rather than that England is not a place.

India is ₹375 a letter; everywhere else is **$12**, in USD — Razorpay raises
USD orders on this account, so a foreign card is charged in dollars rather than
a rupee figure it has to convert.

## Two tables, not one

`subscribers` holds people who have **paid**, and nobody else. A sign-up that
has not been paid for is an *attempt* and lives in `signup_attempts` until the
money clears, at which point it is promoted and the attempt is deleted.

The attempt has to exist before the payment: Razorpay wants an order created
before the customer pays, and if the address lived only in the reader's
browser, a tab that died mid-payment would leave money taken and nowhere to
post to. What it must not do is sit in the subscriber list looking like a
customer — which is what it used to do, as a row with `status = 'pending'`,
and worse, a returning reader's own row was flipped to 'pending' while they
renewed.

`_promote()` in `app/routers/subscriptions.py` is the only place a row enters
`subscribers`. An attempt nobody pays for is swept by
`cycles.discard_abandoned()` after 24 hours and never appears there at all.

A payment starts against `attempt_id` and gains its `subscriber_id` on
promotion, so both columns are nullable and the admin ledger left-joins each.

## Emails

Two go out, both through `app/mail.py`:

- **To Iris**, when a payment clears and when somebody abroad joins the waiting
  list. No `to` argument; lands in the club inbox.
- **To the reader**, once their payment clears — `_confirm_to_reader()`. This
  is the only outward-facing email, and it doubles as their receipt: reference,
  payment id and amount are all in it.

Both are wrapped and non-fatal. By the time either runs the money is banked and
the row is written, so a dead mailbox must never turn a successful payment into
an error for whoever just paid. The admin panel's **Account** tab shows which
way email goes and how the last send turned out.

## Deploying to Render

`render.yaml` in this folder is a Blueprint: **New > Blueprint** in the Render
dashboard, point it at this repo, and it creates the service with the build and
start commands, region, health check and environment already set. It stops to
ask for the values marked `sync: false` — the Neon URL, your site's origin,
the email settings and the three Razorpay keys.

After a deploy that changes `schema.sql`, run the migration from your own
machine — `backend/.env` points at the same Neon database:

```
python -m app.migrate        # safe to run any number of times
python -m app.create_admin   # first time only: makes your admin login
```

Doing it by hand instead? The settings that matter:

| Setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |

`--host 0.0.0.0` is not optional: the default `127.0.0.1` only accepts
connections from inside the container, so Render cannot route to it. And leave
`PORT` alone — Render sets it.

On the free plan the service sleeps after 15 minutes idle and takes most of a
minute to wake. The site covers for this by pinging `/api/config` as soon as
any page loads, so the wake happens while the reader is still reading.

## Configuration

Everything lives in `backend/.env` — see `.env.example` for the full list.

| Variable | What it does |
| --- | --- |
| `DATABASE_URL` | Neon connection string (pooled or direct; the `postgresql+psycopg://` form Neon offers for SQLAlchemy is accepted too) |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the API. No trailing slash — a browser never sends one |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | **Empty until the account is approved** |

## A note on the pinned versions

`requirements.txt` pins releases that publish **prebuilt wheels for current
CPython, 3.14 included**. That is not incidental. Render installs a recent
Python by default, and `pydantic-core` and `psycopg-binary` are compiled
extensions: on a version they have no wheel for, pip falls back to building
`pydantic-core` from Rust source, which dies on Render's read-only cargo
registry with a `maturin failed` error that reads like a pip problem.

So when bumping anything here, check the new version publishes a `cp3xx` wheel
rather than only a source tarball. Pinning `PYTHON_VERSION` is the more fragile
fix — it applies only to services created from the Blueprint, and a version
Render does not stock fails the build outright.

## Payments

The Razorpay integration is written and wired up, but switched off until there
are keys for it:

- **While the keys are empty** — `/api/config` reports `paymentsEnabled: false`,
  every sign-up is saved with `status = 'pending'`, and the site shows a "not
  live yet" notice instead of a pay button. `/verify` refuses with 503. Nothing
  can be charged.
- **Once the keys are in `.env`** — restart, and the same endpoints start
  creating real Razorpay orders. `POST /api/subscriptions` returns
  `payment.enabled: true` with a `key_id` and `order_id`, the site opens
  Checkout, and `/verify` confirms the signature before marking a row paid.

Switching it on is filling in two values and restarting. No code change.

## The webhook

The browser callback (`/verify`) is the normal path, but it only runs if the
reader's tab survives long enough to fire it. Someone who pays and then closes
the window, or loses signal on the way back, would otherwise be charged with
nothing recorded. Razorpay reports the same payment to
`POST /api/payments/webhook` server-to-server, so the purchase lands either way.

Both routes credit through the same `_credit()` step, and it is guarded by
`where status = 'pending'` — so the callback, a retry of it, and the webhook can
all arrive and only the first one credits anything. Without that a reader who
paid once could end up owed six letters for a three-letter plan.

Set it up at **Razorpay Dashboard > Settings > Webhooks**:

| Field | Value |
| --- | --- |
| Webhook URL | `https://<your-service>.onrender.com/api/payments/webhook` |
| Secret | A random string you invent, also set as `RAZORPAY_WEBHOOK_SECRET` |
| Active Events | `payment.captured` and `payment.failed` |

The secret is not something Razorpay gives you — it is a shared password you
choose, used to sign each delivery so nobody else can post fake payments at the
API. An unsigned or wrongly signed request gets a 403. Anything else — an
unknown order, an event we do not handle — is answered 200, because Razorpay
retries non-2xx replies and eventually disables a webhook that keeps failing.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness plus a database ping |
| `GET` | `/api/config` | Everything the page reads at load: the edition on sale (open or sold out), prices, this edition's envelope and photo, the gallery |
| `GET` | `/api/media/{id}` | One photograph, cached for good (a new upload is a new id) |
| `POST` | `/api/subscriptions` | Create or update a sign-up — refused with 409 while the edition is sold out |
| `GET` | `/api/subscriptions/{id}` | Read one back |
| `POST` | `/api/subscriptions/{id}/order` | Re-open checkout for an unpaid sign-up |
| `POST` | `/api/subscriptions/{id}/verify` | Confirm a Razorpay payment (signature checked) |
| `POST` | `/api/payments/webhook` | Razorpay's own report of the same payment |
| `POST` | `/api/international-interest` | "Tell me when you post to my country" |

### The admin panel's API

The panel lives at `/admin` on the site. Every route below except `/login`
needs `Authorization: Bearer <token>`, the token `/login` hands back.

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/admin/login` | Username + password → a 12-hour session. Ten tries per 15 minutes per address |
| `POST` | `/api/admin/logout` | End this session |
| `POST` | `/api/admin/password` | Change your password; signs out every other session |
| `GET` | `/api/admin/overview` | Everything the dashboard shows |
| `PUT` | `/api/admin/edition` | `{cycle, status}` — which edition is on sale, open or sold out. Moving forward counts the old edition's envelopes |
| `PUT` | `/api/admin/editions/{cycle}` | `{items, envelope_media_id}` — what is in that envelope, and its photo |
| `GET` | `/api/admin/editions/{cycle}/export` | That edition's readers as Excel (Signed up / To post) |
| `GET` | `/api/admin/subscriptions` | Search readers |
| `POST` | `/api/admin/subscriptions/{id}/status` | Cancel or reinstate a reader |
| `PUT` | `/api/admin/plans/{region}/{months}` | Change a price (per month, in paise or cents) |
| `POST` | `/api/admin/media?kind=gallery\|envelope&caption=` | Upload a photo — the body is the image itself |
| `DELETE` | `/api/admin/media/{id}` | Delete a photo |

**Security, in short.** Passwords are stored as salted scrypt hashes, never as
text; accounts are made only with `python -m app.create_admin`, so there is no
sign-up page to attack. Sessions are random tokens of which only a SHA-256 is
stored. A wrong username and a wrong password get the same answer in the same
time. Uploads are checked by their own bytes — JPEG, PNG and WebP only, 5 MB
at most, never SVG. The Excel export writes anything a reader typed as text,
never as a formula. Admin responses are `Cache-Control: no-store`, and every
response carries `nosniff`, `X-Frame-Options: DENY` and `no-referrer`.

## What is and is not stored

**Nothing is kept for readers outside India.** There is no shipping abroad yet,
so the site says so and stops — no form, no request, no row. A direct POST with
`region: "international"` is refused with a 400. Nothing is promised, so nothing
needs keeping to honour it.

**A sign-up that is never paid for does not survive the day.** A row does have to
exist while checkout is in flight — Razorpay wants the order created before the
customer pays, and if the address lived only in the browser, a tab that died
mid-payment would leave money taken and nowhere to post to. So the row is
written first and cleaned up after: anything still `pending` 24 hours later is
discarded by `discard_abandoned()`, which rides the same hook as the monthly
sweep.

Two kinds of leftover, treated differently:

* A **first-timer** who never paid is deleted outright — nothing is lost.
* A **returning reader** who abandoned a renewal is kept and put back to
  `expired`. Only the abandoned attempt is dropped; their paid history is not
  ours to throw away.

So in practice the only rows you ever see are `active` and `expired`.

## Reminders

Somebody arriving between windows can leave an Instagram handle at
`POST /api/reminders` and be nudged when sign-ups open. The request is always
recorded; emailing you is layered on top.

Three things stop it being a nuisance:

* **One per handle per month.** A unique index on the folded handle and the
  delivery month keeps the first request, so tapping the button again is a
  friendly no-op rather than a second email.
* **Three per hour per address** (`app/ratelimit.py`). A burst from one person
  is cut off with a 429 and a `Retry-After`; everybody else is unaffected. The
  counters are in memory, so a restart forgets them — fine for guarding an
  inbox, and worth replacing with a shared store if this ever runs on more than
  one instance. Sign-ups are limited too, at twenty an hour.
* **Mail can never cost somebody their place.** The row is committed before the
  send is attempted, and the send is wrapped, so a dead mailbox cannot turn a
  request that already worked into an error.

Subjects are titled by the delivery month — *"October reminder — @handle"* — so
a season's worth sits together in the inbox.

Read the waiting list at `GET /api/admin/reminders`, and mark one off with
`POST /api/admin/reminders/{id}/done` once you have messaged them.

### Turning the email on

Requests are saved with or without this; these three only decide whether you
are told by email as well.

1. Gmail needs an **app password**, not your account password. Enable 2-Step
   Verification, then Google Account > Security > 2-Step Verification >
   App passwords, and generate one for "Mail".
2. Put it in `backend/.env`:
   ```
   EMAIL_USER=you@gmail.com
   EMAIL_PASSWORD=the-16-character-app-password
   EMAIL_TO=you@gmail.com
   ```
3. Add the same three in Render > your service > Environment, and restart.

`GET /api/admin/reminders` reports `emailSending`, so you can see at a glance
whether it took.

## Who is who

A reader is identified by **first name + phone number**, both reduced to a
stable key (`app/identity.py`) before anything is compared. The same person
writes their own number five ways across five months — `+91 98765 43210`,
`09876543210`, `98765-43210` — and all of them reduce to `9876543210`.

That pairing is a unique index, so one reader is one row however often they
come back. Two names on one number are two readers, which is how a household
sharing a phone works.

**Why the name as well as the phone.** The two mistakes are not equally bad.
Phone alone can *merge* a parent and child on one number into a single record —
one address, two people's payments tangled together. Adding the name can
instead *split* one person in two if they sign up as "Bob" and later as
"Robert". A split is a five-minute tidy-up; a wrong merge posts an envelope to
the wrong house. The key errs towards splitting.

**Signing up twice.** The rule is about whether letters are still owed:

| Situation | What happens |
| --- | --- |
| Same name + phone, letters still to come | **Refused** — 409, naming how many are left, suggesting a different name for a housemate |
| Same name + phone, unpaid attempt | Allowed — the attempt is updated, not duplicated |
| Same name + phone, subscription run out | **Renewal** — the same row is reused, details refreshed |
| Different name, same phone | A separate reader |

Renewals **add**: two letters still owed plus three bought makes five. Assigning
instead of adding would quietly swallow what they had already paid for.

## How a subscription lives

Three tables carry it.

**`plans`** is the price list — one row per region and length. Amounts are
integers in *minor units* (paise, cents), so nothing is ever a float and
Razorpay gets exactly the number it wants. Change a price and only new sign-ups
see it: a subscriber row records what was actually charged, and history must not
be rewritten by a later price change. From October 2026: India ₹549 / ₹500 /
₹450 a month for 1 / 3 / 12 months; abroad $13 for one letter.

**`cycles`** is one row per *edition* — the month an envelope goes out — with
its status (`open` or `sold_out`) and what is in that envelope. **There are no
sign-up dates any more.** Which edition is on sale is set by hand in the admin
panel and kept in `current_edition`; every purchase records the edition it
bought into, in `payments.cycle`.

**`subscribers`** carries `plan_months` (1, 3, 6 or 12) and `deliveries_remaining`,
a counter that starts at the plan length *when the payment clears*, not at
sign-up — an unpaid row owes nothing and can never appear on a mailing list.

Each month, one statement counts a delivery against everyone:

```sql
update subscribers set
    deliveries_remaining = greatest(deliveries_remaining - 1, 0),
    last_counted_cycle   = :cycle,
    status = case when deliveries_remaining - 1 <= 0 then 'expired' else 'active' end
where status = 'active'
  and (last_counted_cycle is null or last_counted_cycle < :cycle)
```

3 becomes 2 becomes 1 becomes 0, and 0 expires. Being a single statement it is
atomic — a dropped connection cannot leave half the list decremented — and
`last_counted_cycle < :cycle` makes it idempotent, so running it twice for the
same month is a no-op the second time.

**When it runs.** An edition is counted when the panel *moves past it* — when
November goes on sale, October is counted (`cycles.close_edition()`). Marking
October sold out does not count it; only moving on does. Only the edition that
was on sale is counted, since it is the only one anybody could have bought
into.

The export still lists October's readers after the count: its **To post** sheet
takes in everyone counted *for* October, so the list is right while the
envelopes are still being packed and the site has already moved on.

A payment that clears after its edition was counted — begun for October,
finished after November opened — joins the edition on sale instead, so it is
never counted for an envelope already packed without it.

**Housekeeping.** `cycles.sweep()` discards sign-ups abandoned for 24 hours and
expired admin sessions. It rides on `GET /api/config` and the panel's overview,
since Render's free plan has no cron.

**Expired, not deleted.** An expired subscription drops off every list
immediately, but the row stays. A Razorpay chargeback can arrive months after
the fact and that row is the evidence.

## What gets stored

One row per reader per month in `subscribers` — name, email, phone, Instagram
handle, full postal address with PIN code, optional birthday, optional
interests, and the payment state. See `app/schema.sql`.

Re-submitting the same email in the same sign-up window updates that row rather
than making a second one. An email that has already paid this month is refused
with a 409.

## Notes

The connection pool is psycopg's **synchronous** one, with every query handed to
a worker thread (`app/db.py`). That is deliberate: psycopg's async mode refuses
to run on Windows' default ProactorEventLoop, and which loop you get depends on
how uvicorn was launched — `--reload` gives a selector loop and works, plain
`uvicorn app.main:app` gives a proactor loop and fails with *"Psycopg cannot use
the 'ProactorEventLoop'"*. The sync pool behaves the same everywhere, under any
launch mode, and the callers still just `await fetch_one(...)`.
