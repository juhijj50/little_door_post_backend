-- The Little Door Post — schema. Safe to run repeatedly:  python -m app.migrate
--
-- The main tables:
--   plans        the price list — what a 1, 3 or 12 month subscription costs
--   cycles       one row per edition (delivery month): open or sold out, and
--                what goes in that month's envelope
--   current_edition  which edition is on sale right now — set by hand
--   media        photographs: the gallery, and each edition's envelope
--   admin_users / admin_sessions  who can sign in to /admin
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
-- price changed since. The rate card itself is set once, further down, under
-- `data_migrations` — prices are edited from the admin panel now, and a
-- migration that reset them on every run would quietly undo those edits.
insert into plans (region, months, currency, amount_minor) values
    ('india',          1, 'INR',  37500),
    ('india',          3, 'INR',  34500),
    ('india',          6, 'INR',  31500),
    ('international',  1, 'USD',   1500),
    ('international',  3, 'USD',   1400),
    ('international',  6, 'USD',   1300)
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


-- ════════════════════════════════════════════════════════════════════════════
-- October 2026: editions run by hand, the new rate card, photographs, and a
-- proper sign-in for the admin panel.
-- ════════════════════════════════════════════════════════════════════════════

-- ── one-off data changes ────────────────────────────────────────────────────
-- Each runs once, recorded here by name, so re-running the migration never
-- repeats it. That matters for prices above all: they are edited from the
-- admin panel now, and a statement that set them on every run would quietly
-- put back whatever this file said.
create table if not exists data_migrations (
    name        text primary key,
    applied_at  timestamptz not null default now()
);


-- ── twelve months ───────────────────────────────────────────────────────────
alter table plans drop constraint if exists plans_months_check;
alter table plans add constraint plans_months_check check (months in (1, 3, 6, 12));

alter table subscribers drop constraint if exists subscribers_plan_months_check;
alter table subscribers add constraint subscribers_plan_months_check
    check (plan_months is null or plan_months in (1, 3, 6, 12));


-- ── the rate card, from October 2026 ────────────────────────────────────────
-- India, per month:  1 month ₹549 · 3 months ₹500 (₹1,500) · 12 months ₹450
-- (₹5,400). Six months is switched off rather than deleted, because readers
-- already on one are owed envelopes at the price they paid.
-- Abroad: one letter for $13, postage included. No longer plans.
do $$
begin
    if not exists (select 1 from data_migrations where name = '2026-10-rate-card') then
        insert into plans (region, months, currency, amount_minor, active)
        values ('india', 12, 'INR', 45000, true)
        on conflict (region, months) do nothing;

        update plans set amount_minor = v.rate, active = v.on_sale, updated_at = now()
          from (values
                  ('india',          1, 54900, true),
                  ('india',          3, 50000, true),
                  ('india',          6, 31500, false),
                  ('india',         12, 45000, true),
                  ('international',  1,  1300, true),
                  ('international',  3,  1400, false),
                  ('international',  6,  1300, false)
               ) as v(region, months, rate, on_sale)
         where plans.region = v.region and plans.months = v.months;

        insert into data_migrations (name) values ('2026-10-rate-card');
    end if;
end $$;


-- ── photographs ──────────────────────────────────────────────────────────────
-- Kept in the database rather than on disk: Render's free tier wipes its disk
-- on every deploy and every restart, so an uploaded file would vanish within
-- the day. The admin panel shrinks a photo before it is sent, so a row is a
-- few hundred kilobytes, not the 5 MB a phone takes.
--
-- Only these three types are accepted, and the type is read from the file's
-- own first bytes on upload rather than trusted from the browser. No SVG: an
-- SVG can carry script.
create table if not exists media (
    id            uuid primary key default gen_random_uuid(),
    kind          text not null check (kind in ('gallery', 'envelope')),
    content_type  text not null check (content_type in ('image/jpeg', 'image/png', 'image/webp')),
    data          bytea not null,
    byte_size     integer not null,
    caption       text,
    created_at    timestamptz not null default now()
);

create index if not exists media_gallery_idx on media (created_at desc) where kind = 'gallery';


-- ── editions ─────────────────────────────────────────────────────────────────
-- An edition is one month's envelope, filed under `cycles` as before. Sign-up
-- dates no longer decide anything: which edition is on sale, and whether it
-- has sold out, is set by hand from the admin panel. The date columns stay for
-- the history of the months that ran by them, and are simply left empty now.
alter table cycles alter column opens_at  drop not null;
alter table cycles alter column closes_at drop not null;

alter table cycles add column if not exists status text not null default 'open';
alter table cycles drop constraint if exists cycles_status_check;
alter table cycles add constraint cycles_status_check check (status in ('open', 'sold_out'));

-- What is in this month's envelope: [{"title": "...", "detail": "..."}, ...],
-- shown on the site in that order. Empty means "the same as last month".
alter table cycles add column if not exists contents jsonb;
alter table cycles add column if not exists envelope_media_id uuid;
alter table cycles drop constraint if exists cycles_envelope_media_fk;
alter table cycles add constraint cycles_envelope_media_fk
    foreign key (envelope_media_id) references media (id) on delete set null;

-- Which edition is on sale. One row, ever — the primary key can only be true.
create table if not exists current_edition (
    singleton   boolean primary key default true check (singleton),
    cycle       text not null references cycles (cycle),
    updated_at  timestamptz not null default now()
);

-- First run only: carry on with the edition the old date rule was selling,
-- which on the 1st–5th is this month and from the 6th is next month.
do $$
declare
    ist timestamp := now() at time zone 'Asia/Kolkata';
    selling text;
begin
    if not exists (select 1 from current_edition) then
        selling := to_char(
            case when extract(day from ist) <= 5 then ist else ist + interval '1 month' end,
            'YYYY-MM');
        insert into cycles (cycle, status) values (selling, 'open')
        on conflict (cycle) do nothing;
        insert into current_edition (cycle) values (selling);
    end if;
end $$;


-- ── the admin panel's sign-in ───────────────────────────────────────────────
-- Passwords are stored as scrypt hashes (see app/auth.py), never as text.
-- Accounts are made from the command line, not from the web:
--     python -m app.create_admin
create table if not exists admin_users (
    id             uuid primary key default gen_random_uuid(),
    username       text not null,
    password_hash  text not null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    last_login_at  timestamptz
);
create unique index if not exists admin_users_username_idx on admin_users (lower(username));

-- A signed-in browser. Only a SHA-256 of the token is kept, so a copy of this
-- table is not a way in.
create table if not exists admin_sessions (
    token_hash   text primary key,
    user_id      uuid not null references admin_users (id) on delete cascade,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null,
    ip           text
);
create index if not exists admin_sessions_expiry_idx on admin_sessions (expires_at);


-- ── founding members pay the twelve-month rate ──────────────────────────────
-- From October 2026 a founding member's monthly rate is the India twelve-month
-- rate, read live from `plans` by app/founding.py — so it follows that price
-- when it is changed in the admin panel. The stored rate is brought in line
-- once, for the record and as the fallback.
alter table founding_members alter column rate_minor set default 45000;
do $$
begin
    if not exists (select 1 from data_migrations where name = '2026-10-founding-rate') then
        update founding_members set rate_minor = 45000;
        insert into data_migrations (name) values ('2026-10-founding-rate');
    end if;
end $$;


-- ── deliveries: one row per reader per edition counted ──────────────────────
-- The full history of who was counted for which edition. Before this, only
-- the latest month was kept (`subscribers.last_counted_cycle`), so an older
-- edition's posting list lost its longer-plan readers as soon as the next
-- month was counted, and a mistaken "Open next month" could not be undone.
--
-- The primary key is the guard against counting anyone twice: one row per
-- reader per edition, ever. Undoing an edition deletes its rows and gives the
-- envelope back (see cycles.undo_edition).
create table if not exists deliveries (
    subscriber_id  uuid not null references subscribers (id) on delete cascade,
    cycle          text not null,
    counted_at     timestamptz not null default now(),
    primary key (subscriber_id, cycle)
);
create index if not exists deliveries_cycle_idx on deliveries (cycle);

-- What existed before this table: each reader's latest counted month. Earlier
-- months were never recorded, so they cannot be recovered.
do $$
begin
    if not exists (select 1 from data_migrations where name = 'backfill-deliveries') then
        insert into deliveries (subscriber_id, cycle, counted_at)
        select id, last_counted_cycle, updated_at from subscribers
        where last_counted_cycle is not null
        on conflict do nothing;
        insert into data_migrations (name) values ('backfill-deliveries');
    end if;
end $$;
