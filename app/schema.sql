-- The Little Door Post — schema. Safe to run repeatedly:  python -m app.migrate

create extension if not exists pgcrypto;

create table if not exists subscribers (
    id                   uuid primary key default gen_random_uuid(),
    reference            text unique not null,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),

    -- which door they came through
    region               text not null check (region in ('india', 'international')),
    cycle                text not null,              -- sign-up month, 'YYYY-MM'

    -- the reader
    full_name            text not null,
    email                text not null,
    phone                text not null,
    instagram            text,
    birthdate            date,                       -- optional, for the birthday post
    interests            text[] not null default '{}',
    interests_note       text,

    -- where the envelope goes
    address_line1        text,
    address_line2        text,
    landmark             text,
    city                 text,
    state                text,
    pincode              text,
    country              text not null default 'India',

    -- payment (Razorpay)
    status               text not null default 'pending'
                           check (status in ('pending', 'paid', 'failed',
                                             'cancelled', 'waitlist')),
    amount_inr           integer,
    razorpay_order_id    text,
    razorpay_payment_id  text,
    paid_at              timestamptz,
    admin_note           text
);

create index if not exists subscribers_status_idx     on subscribers (status);
create index if not exists subscribers_email_idx      on subscribers (lower(email));
create index if not exists subscribers_cycle_idx      on subscribers (cycle);
create index if not exists subscribers_created_idx    on subscribers (created_at desc);
create index if not exists subscribers_rzp_order_idx  on subscribers (razorpay_order_id);

-- "Whose birthday is it this month?"
create index if not exists subscribers_birthday_idx
    on subscribers (extract(month from birthdate), extract(day from birthdate))
    where birthdate is not null;
