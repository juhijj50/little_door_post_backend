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

## Deploying to Render

`render.yaml` in this folder is a Blueprint: **New > Blueprint** in the Render
dashboard, point it at this repo, and it creates the service with the build and
start commands, region, health check and environment already set. It stops to
ask for the six values marked `sync: false` — the Neon URL, the admin token,
your site's origin, and the three Razorpay keys (leave those blank for now).

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
| `ADMIN_TOKEN` | Guards `/api/admin/*`. Set it to something long and random |
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
| `GET` | `/api/config` | Price, sign-up window, payment availability, envelope contents |
| `POST` | `/api/subscriptions` | Create or update a sign-up for this month |
| `GET` | `/api/subscriptions/{id}` | Read one back |
| `POST` | `/api/subscriptions/{id}/order` | Re-open checkout for an unpaid sign-up |
| `POST` | `/api/subscriptions/{id}/verify` | Confirm a Razorpay payment (signature checked) |
| `POST` | `/api/payments/webhook` | Razorpay's own report of the same payment |
| `GET` | `/api/admin/subscriptions` | List and search sign-ups |
| `GET` | `/api/admin/subscriptions.csv` | Address labels for the month's paid readers |
| `POST` | `/api/admin/subscriptions/{id}/status` | Set a status by hand |
| `GET` | `/api/admin/birthdays?month=` | Whose birthday falls this month |
| `GET` | `/api/admin/interests` | What readers asked to read about |

Admin routes want the token as an `X-Admin-Token` header, or `?token=` so a
link opens in a browser.

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

**`plans`** is the price list — one row per region and length, six in all. Amounts
are integers in *minor units* (paise, cents), so nothing is ever a float and
Razorpay gets exactly the number it wants. Change a price and only new sign-ups
see it: a subscriber row records what was actually charged, and history must not
be rewritten by a later price change.

**`cycles`** is one row per delivery month, holding that month's sign-up window.
Rows are created on demand from the default rule — the 15th to the 5th, in
Indian time — and can then be edited. **Whatever the table says wins**, so
`PUT /api/admin/cycles/2026-11` with different dates moves that one month and
nothing else.

**`subscribers`** carries `plan_months` (1, 3 or 6) and `deliveries_remaining`,
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

**When it runs matters.** A month is counted once the *next* window opens on the
15th — not when its own window shuts on the 5th. The envelopes have not been
posted on the 5th, and expiring a one-letter reader then would drop them off the
mailing list before the letter they paid for was ever sent. Counting on the 15th
leaves ten days to pack and post.

That is what lets it run automatically without a scheduler: every request that
resolves the current cycle sweeps any month now due (`cycles.sweep()`). Render's
free plan has no cron, and a sleeping service would miss one anyway.
`POST /api/admin/sweep` and `POST /api/admin/cycles/{cycle}/roll` do the same
thing by hand — the latter for when you have posted early.

**Where the housekeeping runs.** `cycles.sweep()` does both jobs — rolling over
any month now due, and discarding abandoned sign-ups — and is called from six
places: `GET /api/config` (so every page load triggers it), `POST /api/subscriptions`,
`GET /api/admin/cycles`, `POST /api/admin/sweep`, and both mailing-list
endpoints.

The mailing-list ones matter most. They are read exactly when the site has been
quietest — between windows, when no visitor has triggered anything — and they
are what you print labels from. Sweeping there first is what stops a reader
whose subscription has quietly run out from getting an envelope nobody paid for.

**Expired, not deleted.** An expired subscription drops off every list
immediately, but the row stays. A Razorpay chargeback can arrive months after
the fact and that row is the evidence. `POST /api/admin/purge` really deletes,
but it needs `?confirm=true`, refuses anything under 30 days old, and never
touches an active or pending row.

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
