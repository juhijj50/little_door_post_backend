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

## Configuration

Everything lives in `backend/.env` — see `.env.example` for the full list.

| Variable | What it does |
| --- | --- |
| `DATABASE_URL` | Neon connection string (pooled or direct; the `postgresql+psycopg://` form Neon offers for SQLAlchemy is accepted too) |
| `ADMIN_TOKEN` | Guards `/api/admin/*`. Set it to something long and random |
| `SUBSCRIPTION_PRICE_INR` | Price of one month's envelope |
| `SIGNUP_WINDOW` | `Automatic` (20th → 2nd), `Open now`, or `Closed` |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the API |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | **Empty until the account is approved** |

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

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness plus a database ping |
| `GET` | `/api/config` | Price, sign-up window, payment availability, envelope contents |
| `POST` | `/api/subscriptions` | Create or update a sign-up for this month |
| `GET` | `/api/subscriptions/{id}` | Read one back |
| `POST` | `/api/subscriptions/{id}/order` | Re-open checkout for an unpaid sign-up |
| `POST` | `/api/subscriptions/{id}/verify` | Confirm a Razorpay payment (signature checked) |
| `GET` | `/api/admin/subscriptions` | List and search sign-ups |
| `GET` | `/api/admin/subscriptions.csv` | Address labels for the month's paid readers |
| `POST` | `/api/admin/subscriptions/{id}/status` | Set a status by hand |
| `GET` | `/api/admin/birthdays?month=` | Whose birthday falls this month |
| `GET` | `/api/admin/interests` | What readers asked to read about |

Admin routes want the token as an `X-Admin-Token` header, or `?token=` so a
link opens in a browser.

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
