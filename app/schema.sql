-- The Little Door Post — schema. Safe to run repeatedly:  python -m app.migrate
--
-- Four tables:
--   plans        the price list — what a 1, 3 or 6 month subscription costs
--   cycles       one row per delivery month, holding that month's sign-up window
--   subscribers  ONE ROW PER READER, identified by first name + phone
--   payments     one row per purchase, appended and never rewritten
--   founding     September's readers, who keep the founding rate for good

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

-- `amount_minor` is the rate PER MONTH, not the total. One letter arrives each
-- month, so a longer plan is a longer commitment at a better monthly rate —
-- never a bundle of letters bought at once. The charge is rate × months.
--
-- `do nothing` on conflict, so re-running this migration never overwrites a
-- price changed since. The update below is the deliberate exception: it resets
-- India to the current rate card.
insert into plans (region, months, currency, amount_minor) values
    ('india',          1, 'INR',  37500),   -- ₹375 a month
    ('india',          3, 'INR',  34500),   -- ₹345 a month  → ₹1035
    ('india',          6, 'INR',  31500),   -- ₹315 a month  → ₹1890
    ('international',  1, 'USD',   1500),   -- placeholder, to be set later
    ('international',  3, 'USD',   1400),
    ('international',  6, 'USD',   1300)
on conflict (region, months) do nothing;

-- What is on sale in India, and at what rate. `active` is the switch the site
-- reads: /api/config only lists active plans, and create_subscription refuses
-- a plan that is not one, so an inactive row cannot be bought even by posting
-- straight at the API.
--
-- 20 Sep 2026: only the single month is on sale. The three- and six-month
-- plans are switched off while the rate card is reworked for next month. Their
-- rates are left as they were, because the readers already on them are owed
-- envelopes at the price they paid, and a rate nobody can buy does no harm.
--
-- Next month's card is written up in react-app/src/business.js under
-- `nextRateCard`: 499 / 1380 / 2640 / 5040 for 1 / 3 / 6 / 12 months. The
-- twelve-month row does not exist yet and the `months` check below has to gain
-- a 12 before it can, along with ALLOWED_MONTHS in app/plans.py.
update plans set amount_minor = v.rate, active = v.on_sale, updated_at = now()
  from (values (1, 37500, true), (3, 34500, false), (6, 31500, false))
       as v(months, rate, on_sale)
 where plans.region = 'india' and plans.months = v.months
   and (plans.amount_minor <> v.rate or plans.active <> v.on_sale);

-- International is not sold yet and these are placeholders, but they are left
-- descending so the rate card is never nonsense if it is ever shown.
update plans set amount_minor = v.rate, active = false, updated_at = now()
  from (values (1, 1500), (3, 1400), (6, 1300)) as v(months, rate)
 where plans.region = 'international' and plans.months = v.months
   and (plans.amount_minor <> v.rate or plans.active);


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
--
-- Superseded further down: an open payment now belongs to a signup_attempts
-- row, because the subscriber does not exist until the money clears. Left here
-- only so the history of this file reads straight; it is dropped below.

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

drop table if exists reminders;


-- ── founding members ────────────────────────────────────────────────────────
-- September's readers, who keep the founding rate however short a plan they
-- take. Matched on the phone number rather than the code alone: the code is
-- shareable, a number is not, so quoting FOUNDING15 only works from the phone
-- it belongs to.
create table if not exists founding_members (
    phone_key   text primary key,            -- normalised, as in app/identity.py
    first_name  text not null,
    last_name   text,
    code        text not null default 'FOUNDING15',
    rate_minor  integer not null default 31500,   -- ₹315 a month, any length
    note        text,
    created_at  timestamptz not null default now()
);

-- The founding rate follows the six-month rate, which moved to ₹315 in
-- September 2026. Only rows still sitting at the old ₹300 are touched, so
-- re-running this is a no-op and a hand-set rate is never overwritten.
update founding_members set rate_minor = 31500 where rate_minor = 30000;

create index if not exists founding_code_idx on founding_members (code);


-- ── the reader, in more pieces ──────────────────────────────────────────────
-- A name split in two so the envelope can be addressed properly, and a phone
-- split from its country code so the number is comparable across the ways
-- people write it.
alter table subscribers add column if not exists first_name   text;
alter table subscribers add column if not exists last_name    text;
alter table subscribers add column if not exists phone_cc     text default '+91';
alter table subscribers add column if not exists phone_number text;

update subscribers set
    first_name = coalesce(first_name, split_part(btrim(full_name), ' ', 1)),
    last_name  = coalesce(last_name,
                          nullif(btrim(substr(btrim(full_name),
                                 length(split_part(btrim(full_name), ' ', 1)) + 1)), ''))
 where first_name is null;

-- What they were charged per month, and under what code. Kept on the row so a
-- later rate change never rewrites what somebody actually paid.
alter table subscribers add column if not exists rate_minor  integer;
alter table subscribers add column if not exists promo_code  text;

-- A subscription bought for somebody else, and the note to tuck in with it.
alter table subscribers add column if not exists is_gift      boolean not null default false;
alter table subscribers add column if not exists gift_message text;

alter table payments add column if not exists rate_minor integer;
alter table payments add column if not exists promo_code text;


-- ── international interest ──────────────────────────────────────────────────
-- Readers outside India who want to be told when posting abroad opens.
--
-- Nothing is sold here and no address is taken: the export process is still
-- being set up, so all this records is who asked and where they are, which is
-- also what decides which countries are worth opening first.
--
-- `instagram_key` is the handle folded to lower case and is the primary key, so
-- asking twice updates the one row rather than making a second. Somebody who
-- signs up, forgets, and signs up again a week later is one person who is keen,
-- not two people.
create table if not exists international_interest (
    instagram_key  text primary key,
    instagram      text not null,            -- as they typed it
    country        text not null,
    email          text,
    note           text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    notified_at    timestamptz               -- set by hand once they are told
);

create index if not exists international_interest_country_idx
    on international_interest (country);
create index if not exists international_interest_waiting_idx
    on international_interest (created_at) where notified_at is null;


-- ── signup attempts ─────────────────────────────────────────────────────────
-- A sign-up that has not been paid for yet.
--
-- These used to be written straight into `subscribers` with status 'pending',
-- which meant the subscriber list held people who had only ever opened the
-- form — and, worse, a returning reader's own row was flipped to 'pending'
-- while they were partway through renewing. `subscribers` now holds paid
-- readers and nobody else.
--
-- The row still has to exist before the money moves, and that is not
-- negotiable: Razorpay wants an order created before the customer pays, and if
-- the address lived only in the reader's browser, a tab that died mid-payment
-- would leave money taken and nowhere to post to. So the attempt is written
-- here, and promoted into `subscribers` the moment a payment clears.
--
-- `subscriber_id` is set when the attempt belongs to a reader who has
-- subscribed before, so the promotion knows to top up that row rather than
-- start a second one for the same person.
create table if not exists signup_attempts (
    id                uuid primary key default gen_random_uuid(),
    reference         text unique not null,     -- shown during checkout, kept on promotion
    subscriber_id     uuid references subscribers (id) on delete cascade,

    region            text not null check (region in ('india', 'international')),
    cycle             text not null,
    name_key          text not null,
    phone_key         text not null,

    full_name         text not null,
    first_name        text not null,
    last_name         text,
    email             text not null,
    phone             text not null,
    phone_cc          text,
    phone_number      text,
    instagram         text,
    birthdate         date,
    interests         text[] not null default '{}',
    interests_note    text,

    address_line1     text,
    address_line2     text,
    landmark          text,
    city              text,
    state             text,
    pincode           text,
    country           text not null default 'India',

    plan_months       smallint not null default 1,
    currency          text not null default 'INR',
    amount_minor      integer not null,
    rate_minor        integer,
    promo_code        text,
    is_gift           boolean not null default false,
    gift_message      text,

    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- One open attempt per person per month. A reader who fills the form in twice
-- updates the one attempt instead of leaving a trail of them.
create unique index if not exists signup_attempts_person_idx
    on signup_attempts (name_key, phone_key, cycle);
create index if not exists signup_attempts_stale_idx on signup_attempts (updated_at);

-- A payment now starts life against an attempt and gains its subscriber only
-- when it clears, so the old NOT NULL no longer holds.
alter table payments add column if not exists attempt_id uuid
    references signup_attempts (id) on delete set null;
alter table payments alter column subscriber_id drop not null;

-- The old index keyed an open payment to a subscriber that no longer exists
-- at that point. Dropped by name, and replaced by one keyed on the attempt.
-- Note the name differs from the old one on purpose: reusing it would leave
-- `create ... if not exists` silently keeping the old definition.
drop index if exists payments_open_attempt_idx;
create unique index if not exists payments_open_per_attempt_idx
    on payments (attempt_id) where status = 'pending';
