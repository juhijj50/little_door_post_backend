"""Editions: which month's envelope is on sale, and what happens when it moves on.

An *edition* is the month an envelope goes out, written 'YYYY-MM' and filed in
the `cycles` table. Which edition is on sale, and whether it has sold out, is
set by hand from the admin panel — there are no sign-up dates any more:

    October 2026 · open       sign-ups buy into the October envelope
    October 2026 · sold out   the site says October is sold out, November
                              opens soon, and sign-ups are refused
    November 2026 · open      selling again, now for November

Moving the edition forward is also what counts the envelopes: every edition
before the new one is rolled over, so a one-month October reader is finished
and a three-month one has two to go. That used to happen on a timer, ten days
after a date-based window closed; with the dates gone, the edition moving on
is the one moment that reliably means "that month is done".
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .db import fetch_all, fetch_one

log = logging.getLogger("littledoorpost.cycles")

# Indian time, for the one place a date is still worked out: the fallback if
# the current edition has somehow gone missing. India has no daylight saving,
# so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))

CYCLE_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

STATUSES = ("open", "sold_out")

# What is in an envelope until an edition says otherwise. Every edition can
# have its own list, edited from the admin panel; a new edition starts with a
# copy of the last one's. This is only what the very first edition starts from.
#
# The pieces are named, every one of them. A subscription whose contents are a
# surprise reads to a payment aggregator as a category it does not support, and
# this business has been turned down on exactly that ground once already — so
# whatever an edition lists, it should stay a list of real, printed things.
DEFAULT_CONTENTS = [
    {"title": "A letter from Iris",
     "detail": "Two printed pages from the corner of the world she has wandered into this month."},
    {"title": "A letter from someone she met",
     "detail": "One printed page from a person who lives there — or lived there, long ago."},
    {"title": "A place sticker",
     "detail": "A die-cut sticker of that month's corner of the world."},
    {"title": "A character sticker",
     "detail": "A die-cut sticker of Iris, or of somebody she met on the way."},
    {"title": "An art print",
     "detail": "A small illustrated print on card, drawn from that month's place."},
    {"title": "An activity sheet",
     "detail": "One page — a puzzle, a recipe from that place, or something to make."},
    {"title": "A special poem",
     "detail": "Written for that month by a friend of Iris, and printed to keep."},
    {"title": "A stamp of the place",
     "detail": "A printed paper stamp of that month's corner of the world, for your passport."},
    {"title": "A Wanderland Passport",
     "detail": "For first-time subscribers. A booklet with a page for every door, "
               "and a stamp to paste in each time a letter lands."},
]


def valid_cycle(cycle: str) -> bool:
    return bool(CYCLE_RE.match(cycle or ""))


def shift(cycle: str, by: int) -> str:
    """'2026-12', +1 -> '2027-01'."""
    year, month = (int(p) for p in cycle.split("-"))
    index = year * 12 + (month - 1) + by
    return f"{index // 12}-{index % 12 + 1:02d}"


def month_name(cycle: str) -> str:
    """'2026-10' -> 'October 2026'. The key is how the database files a month,
    not how anybody reads one."""
    try:
        year, month = (int(part) for part in str(cycle).split("-")[:2])
        return f"{MONTH_NAMES[month - 1]} {year}"
    except (ValueError, IndexError):
        return str(cycle)


def describe(row: dict) -> dict:
    """An edition as the site and the panel read it."""
    cycle = row["cycle"]
    return {
        "cycle": cycle,
        "name": month_name(cycle),
        "status": row["status"],
        "open": row["status"] == "open",
        "next": {"cycle": shift(cycle, 1), "name": month_name(shift(cycle, 1))},
    }


def _fallback_cycle(now: datetime | None = None) -> str:
    """The edition the old date rule would be selling. Only used if the
    current-edition row is missing, which the migration makes sure it is not."""
    now = (now or datetime.now(IST)).astimezone(IST)
    cycle = f"{now.year}-{now.month:02d}"
    return cycle if now.day <= 5 else shift(cycle, 1)


async def current() -> dict:
    """The edition on sale right now: its row, plus `open`."""
    row = await fetch_one(
        "select c.* from current_edition e join cycles c on c.cycle = e.cycle"
    )
    if row is None:
        log.error("no current edition set - falling back to the date rule")
        row = await set_current(_fallback_cycle(), "open")
    return {**row, "open": row["status"] == "open"}


async def get(cycle: str) -> dict | None:
    return await fetch_one("select * from cycles where cycle = %s", (cycle,))


async def set_current(cycle: str, status: str) -> dict:
    """Put an edition on sale (or mark it sold out) and make it the current one.

    A month that has never been an edition before starts with a copy of the
    most recent envelope contents, so moving on to November does not leave the
    site listing nothing while the new list is written. Its photograph is not
    copied — that is of one particular envelope.
    """
    row = await fetch_one(
        """
        insert into cycles (cycle, status, contents)
        values (%(cycle)s, %(status)s,
                (select contents from cycles
                  where contents is not null and cycle <> %(cycle)s
                  order by cycle desc limit 1))
        on conflict (cycle) do update set
            status = excluded.status,
            updated_at = now()
        returning *
        """,
        {"cycle": cycle, "status": status},
    )
    await fetch_one(
        """
        insert into current_edition (singleton, cycle) values (true, %s)
        on conflict (singleton) do update set cycle = excluded.cycle, updated_at = now()
        returning cycle
        """,
        (cycle,),
    )
    return row


async def contents_for(row: dict | None) -> list[dict]:
    """What is in an edition's envelope. An edition that has no list of its own
    shows the most recent one that does, then the default."""
    if row and row.get("contents"):
        return row["contents"]
    earlier = await fetch_one(
        "select contents from cycles where contents is not null "
        "and (%s::text is null or cycle <= %s) order by cycle desc limit 1",
        ((row or {}).get("cycle"), (row or {}).get("cycle")),
    )
    return (earlier or {}).get("contents") or DEFAULT_CONTENTS


async def roll_over(cycle: str) -> int:
    """Count one delivery against every active subscriber, and retire the spent.

    One statement, so it either applies to everyone or to no one — a connection
    dropped halfway cannot leave half the list decremented.

    It is also idempotent: each reader counted gets a row in `deliveries`, and
    a reader who already has one for this month is skipped — so running it
    twice is a no-op the second time. (`last_counted_cycle < cycle` stays as a
    second guard: never count a month older than one already counted.)

    Returns how many subscriptions it touched.
    """
    rows = await fetch_all(
        """
        with counted as (
            update subscribers s set
                deliveries_remaining = greatest(deliveries_remaining - 1, 0),
                last_counted_cycle   = %(cycle)s,
                status = case when deliveries_remaining - 1 <= 0 then 'expired' else 'active' end,
                updated_at = now()
            where s.status = 'active'
              and s.cycle <= %(cycle)s
              and (s.last_counted_cycle is null or s.last_counted_cycle < %(cycle)s)
              and not exists (select 1 from deliveries d
                              where d.subscriber_id = s.id and d.cycle = %(cycle)s)
            returning s.id, s.reference, s.status, s.deliveries_remaining
        ),
        logged as (
            insert into deliveries (subscriber_id, cycle)
            select id, %(cycle)s from counted
            on conflict do nothing
            returning subscriber_id
        )
        select c.reference, c.status, c.deliveries_remaining,
               (select count(*) from logged) as logged
        from counted c
        """,
        {"cycle": cycle},
    )

    if rows:
        done = sum(1 for r in rows if r["status"] == "expired")
        log.info("edition %s: counted %d subscriptions, %d finished", cycle, len(rows), done)
    return len(rows)


async def close_edition(cycle: str) -> int:
    """Count the envelopes of the edition that was on sale, as the panel moves
    on past it. Returns how many readers were counted.

    Only that one edition. It is the only one anybody could have bought into,
    so it is the only one with readers not yet counted — an edition whose
    contents were written ahead of time but that never went on sale has nobody
    to count, and must not take an envelope off anyone. A payment that clears
    late, after this has run, joins the edition on sale instead (see _promote
    in routers/subscriptions.py).

    Safe to repeat: roll_over() skips anyone already counted for the month.
    Reversible: undo_edition() gives the envelopes back.
    """
    counted = await roll_over(cycle)
    await fetch_one(
        "update cycles set rolled_over_at = coalesce(rolled_over_at, now()), "
        "updated_at = now() where cycle = %s returning cycle",
        (cycle,),
    )
    return counted


async def last_counted() -> str | None:
    """The most recent edition anybody was counted for — the one Undo works on."""
    row = await fetch_one("select max(cycle) as cycle from deliveries")
    return row["cycle"] if row else None


async def undo_edition(cycle: str) -> int:
    """Take back the count of one edition: every reader counted for it gets
    that envelope back, and the edition goes back on the site as it was.

    For a mistaken "Open next month". Only the most recent counted edition can
    be undone (the admin route checks), so the history underneath stays
    straight. A cancelled reader stays cancelled; an expired one is active
    again, since they are owed that envelope once more.

    Returns how many readers it gave an envelope back to.
    """
    rows = await fetch_all(
        """
        with removed as (
            delete from deliveries where cycle = %(cycle)s returning subscriber_id
        )
        update subscribers s set
            deliveries_remaining = case when s.status = 'cancelled'
                                        then s.deliveries_remaining
                                        else s.deliveries_remaining + 1 end,
            status = case when s.status = 'expired' then 'active' else s.status end,
            -- The latest month still on record for them, now that this one
            -- is not. The delete above is not visible inside this statement,
            -- hence the explicit `<>`.
            last_counted_cycle = (select max(d.cycle) from deliveries d
                                  where d.subscriber_id = s.id and d.cycle <> %(cycle)s),
            updated_at = now()
        from removed r
        where s.id = r.subscriber_id
        returning s.reference
        """,
        {"cycle": cycle},
    )
    await fetch_one(
        "update cycles set rolled_over_at = null, updated_at = now() "
        "where cycle = %s returning cycle",
        (cycle,),
    )
    await fetch_one(
        """
        insert into current_edition (singleton, cycle) values (true, %s)
        on conflict (singleton) do update set cycle = excluded.cycle, updated_at = now()
        returning cycle
        """,
        (cycle,),
    )
    log.info("edition %s: count undone for %d readers", cycle, len(rows))
    return len(rows)


# The posting list for one edition: who that month's envelope goes to.
#
# No forecasting. A list exists only for an edition that is on sale or has
# been posted:
#
#   Counted for it already — a row in `deliveries`. That keeps a past month's
#   list complete for good, longer plans included.
#
#   The edition on sale now — also everyone with envelopes still owed. Opening
#   an edition counts the one before it, so at that moment "still owed" is
#   exactly who this edition goes to: a one-month October reader is at 0 by
#   the time November opens; a six-month one still has five.
#
# An edition not open yet has no list until it is.
COUNTED_SQL = """
    exists (select 1 from deliveries d
            where d.subscriber_id = s.id and d.cycle = %(cycle)s)
    and s.status not in ('cancelled', 'failed')
"""

OWED_SQL = """
    s.status = 'active' and s.deliveries_remaining > 0
    and s.cycle <= %(cycle)s
    and (s.last_counted_cycle is null or s.last_counted_cycle < %(cycle)s)
    and not exists (select 1 from deliveries d
                    where d.subscriber_id = s.id and d.cycle = %(cycle)s)
"""


async def posting_list(cycle: str) -> list[dict]:
    on_sale = (await current())["cycle"] == cycle
    where = f"({COUNTED_SQL}) or ({OWED_SQL})" if on_sale else COUNTED_SQL
    return await fetch_all(
        f"select * from subscribers s where {where} order by s.full_name", {"cycle": cycle}
    )


# How long a half-finished sign-up is kept. Long enough that someone who pays
# and then loses their connection can still be reconciled; short enough that
# abandoned attempts never become clutter.
ABANDONED_AFTER = "24 hours"


async def discard_abandoned() -> dict:
    """Clear out sign-ups that were started and never paid for.

    An attempt has to exist while checkout is in flight — Razorpay wants the
    order created before the customer pays, and if the address lived only in
    their browser, a tab that died mid-payment would leave money taken and
    nowhere to post to. So it is written first and cleaned up after.

    An attempt with a *paid* payment against it is left alone: that is a
    promotion caught mid-flight, not an abandonment.
    """
    gone = await fetch_all(
        f"""
        delete from signup_attempts a
        where a.updated_at < now() - interval '{ABANDONED_AFTER}'
          and not exists (select 1 from payments p
                          where p.attempt_id = a.id and p.status = 'paid')
        returning a.reference
        """
    )

    dropped = await fetch_all(
        f"""
        delete from payments
        where status = 'pending'
          and updated_at < now() - interval '{ABANDONED_AFTER}'
        returning id
        """
    )

    if gone:
        log.info("discarded %d abandoned sign-ups", len(gone))
    return {
        "deleted": [r["reference"] for r in gone],
        "attempts_dropped": len(dropped),
    }


async def sweep() -> None:
    """Housekeeping that rides along on ordinary requests — Render's free plan
    has no cron, and a sleeping service would miss one anyway: abandoned
    sign-ups, and admin sessions that have run out."""
    await discard_abandoned()
    await fetch_all("delete from admin_sessions where expires_at < now() returning token_hash")
