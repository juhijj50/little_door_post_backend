-- The Little Door Post — schema. Safe to run repeatedly:  python -m app.migrate
--
-- Four tables:
--   plans        the price list — what a 1, 3 or 6 month subscription costs
--   cycles       one row per delivery month, holding that month's sign-up window
--   subscribers  ONE ROW PER READER, identified by first name + phone
--   payments     one row per purchase, appended and never rewritten
--   reminders    people who asked to be told when the next window opens

create extension if not exists pgcrypto;


-- ── plans ───────────────────────────────────────────────────────────────────
-- Money is stored in minor units (paise for INR, cents for USD) as an integer.
-- Nothing here is ever a float, so nothing can drift by a rounding error, and
-- Razorpay wants paise anyway.
create table if not exists plans (
    region        text     not null check (region in ('india', 'international')),
    months        smallint not null check (months in (1, 3, 6)),
    currency      text     not null check (currency in ('INR', 'USD')),
    amount_minor  integer  not null check (amount_minor > 0),
    active        boolean  not null default true,
    updated_at    timestamptz not null default now(),
    primary key (region, months)
);

-- Starting prices. `do nothing` on conflict, so re-running this migration never
-- overwrites a price you have changed since.
insert into plans (region, months, currency, amount_minor) values
    ('india',          1, 'INR',  27900),   -- ₹279
    ('india',          3, 'INR',  79900),   -- ₹799
    ('india',          6, 'INR', 149900),   -- ₹1499
    ('international',  1, 'USD',   1500),   -- $15
    ('international',  3, 'USD',   4200),   -- $42
    ('international',  6, 'USD',   7900)    -- $79
on conflict (region, months) do nothing;


-- ── cycles ──────────────────────────────────────────────────────────────────
-- One row per delivery month. `cycle` is the month the envelope goes out, so
-- the window that opens 15 September and closes 5 October fills cycle 2026-10.
--
-- Rows are created on demand with the default window (the 15th to the 5th) and
-- can then be edited. Whatever is in this table wins: change opens_at/closes_at
-- for one month and that month runs to your dates, not the default rule.
create table if not exists cycles (
    cycle          text primary key,                     -- 'YYYY-MM'
    opens_at       timestamptz not null,
    closes_at      timestamptz not null,
    rolled_over_at timestamptz,                          -- set once its envelopes are counted
    note           text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint cycles_window_valid check (closes_at > opens_at)
);

create index if not exists cycles_window_idx  on cycles (opens_at, closes_at);
create index if not exists cycles_pending_idx on cycles (closes_at) where rolled_over_at is null;


-- ── subscribers ─────────────────────────────────────────────────────────────
create table if not exists subscribers (
    id                   uuid primary key default gen_random_uuid(),
    reference            text unique not null,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),

    region               text not null check (region in ('india', 'international')),
    cycle                text not null,              -- the first delivery month

    full_name            text not null,
    email                text not null,
    phone                text not null,
    instagram            text,
    birthdate            date,
    interests            text[] not null default '{}',
    interests_note       text,

    address_line1        text,
    address_line2        text,
    landmark             text,
    city                 text,
    state                text,
    pincode              text,
    country              text not null default 'India',

    status               text not null default 'pending',
    razorpay_order_id    text,
    razorpay_payment_id  text,
    paid_at              timestamptz,
    admin_note           text
);

-- Columns added after the first release. Written as separate statements so the
-- migration works on a fresh database and an existing one alike.
alter table subscribers add column if not exists plan_months          smallint;
alter table subscribers add column if not exists currency             text;
alter table subscribers add column if not exists amount_minor         integer;
-- How many envelopes are still owed. 3 -> 2 -> 1 -> 0, and 0 means finished.
alter table subscribers add column if not exists deliveries_remaining smallint not null default 0;
-- The last cycle counted against this subscription. Makes the monthly roll-over
-- idempotent: running it twice for the same month cannot double-count anyone.
alter table subscribers add column if not exists last_counted_cycle   text;

-- The old single-price column, replaced by plans + amount_minor.
alter table subscribers drop column if exists amount_inr;

alter table subscribers alter column plan_months set default 1;
update subscribers set plan_months = 1 where plan_months is null;
update subscribers set currency = case when region = 'india' then 'INR' else 'USD' end
 where currency is null;

-- 'paid' became 'active' when subscriptions gained a length: a reader is active
-- while envelopes are still owed, and expired once the counter reaches zero.
alter table subscribers drop constraint if exists subscribers_status_check;
update subscribers set status = 'active' where status = 'paid';
-- No 'waitlist': nothing is posted outside India yet, so an international
-- reader is shown a message and nothing is stored for them at all.
update subscribers set status = 'cancelled' where status = 'waitlist';
alter table subscribers add constraint subscribers_status_check
    check (status in ('pending', 'active', 'expired', 'failed', 'cancelled'));

alter table subscribers drop constraint if exists subscribers_plan_months_check;
alter table subscribers add constraint subscribers_plan_months_check
    check (plan_months is null or plan_months in (1, 3, 6));


-- ── identity ────────────────────────────────────────────────────────────────
-- A reader is their first name plus their phone number, both reduced to a
-- stable key (see app/identity.py) so the five ways somebody writes their own
-- number all land on the same row. One reader, one row, however many times they
-- subscribe — which is what stops repeat sign-ups piling up duplicates.
alter table subscribers add column if not exists name_key  text;
alter table subscribers add column if not exists phone_key text;

-- Backfill anything predating these columns, then enforce the pairing.
update subscribers set
    name_key  = lower(split_part(btrim(full_name), ' ', 1)),
    phone_key = right(regexp_replace(phone, '\D', '', 'g'), 10)
 where name_key is null or phone_key is null;

create unique index if not exists subscribers_identity_idx
    on subscribers (name_key, phone_key);


-- ── payments ────────────────────────────────────────────────────────────────
-- The ledger. One row per purchase, so a reader who comes back every month has
-- one subscribers row and a growing list here. Nothing in this table is ever
-- overwritten: a Razorpay chargeback can arrive months later and these rows are
-- the evidence of what was sold and when.
create table if not exists payments (
    id                  uuid primary key default gen_random_uuid(),
    subscriber_id       uuid not null references subscribers(id) on delete cascade,
    cycle               text not null,              -- the delivery month it bought into
    plan_months         smallint not null,          -- envelopes bought
    currency            text not null,
    amount_minor        integer not null,
    status              text not null default 'pending'
                          check (status in ('pending', 'paid', 'failed', 'cancelled')),
    razorpay_order_id   text,
    razorpay_payment_id text,
    paid_at             timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists payments_subscriber_idx on payments (subscriber_id, created_at desc);
create index if not exists payments_cycle_idx      on payments (cycle);
create index if not exists payments_order_idx      on payments (razorpay_order_id);
-- At most one unpaid attempt per reader per month, so retrying a sign-up
-- updates that attempt instead of leaving a trail of abandoned ones.
create unique index if not exists payments_open_attempt_idx
    on payments (subscriber_id, cycle) where status = 'pending';

create index if not exists subscribers_status_idx    on subscribers (status);
create index if not exists subscribers_email_idx     on subscribers (lower(email));
create index if not exists subscribers_cycle_idx     on subscribers (cycle);
create index if not exists subscribers_created_idx   on subscribers (created_at desc);
create index if not exists subscribers_rzp_order_idx on subscribers (razorpay_order_id);

-- The monthly roll-over scans exactly this set, so let it be an index hit.
create index if not exists subscribers_due_idx
    on subscribers (last_counted_cycle) where status = 'active';

-- "Whose birthday is it this month?"
create index if not exists subscribers_birthday_idx
    on subscribers (extract(month from birthdate), extract(day from birthdate))
    where birthdate is not null;


-- ── reminders ───────────────────────────────────────────────────────────────
-- Somebody who arrived while the window was shut and asked to be nudged when it
-- opens. They give an Instagram handle; the reminder goes out as a DM by hand.
--
-- The unique index is the "once" in "set a reminder once": one request per
-- handle per delivery month, so asking twice quietly changes nothing rather
-- than filling the inbox.
create table if not exists reminders (
    id            uuid primary key default gen_random_uuid(),
    instagram     text not null,              -- as typed, for replying to
    instagram_key text not null,              -- folded, for the uniqueness rule
    email         text,                       -- optional, if they left one
    cycle         text not null,              -- the month they were waiting for
    created_at    timestamptz not null default now(),
    notified_at   timestamptz                 -- set once you have messaged them
);

create unique index if not exists reminders_once_idx
    on reminders (instagram_key, cycle);
create index if not exists reminders_waiting_idx
    on reminders (created_at desc) where notified_at is null;
