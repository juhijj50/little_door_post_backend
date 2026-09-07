"""Delivery months: when sign-ups run, and what happens when a month ends.

A *cycle* is the month an envelope goes out, written 'YYYY-MM'. The window that
opens on 15 September and closes on 5 October fills cycle 2026-10.

The dates come from the `cycles` table, not from this code. A row is created on
demand using the default rule — the 15th to the 5th — and can then be edited;
whatever the table says wins. That is the whole point of keeping them in the
database rather than in a constant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db import fetch_all, fetch_one

log = logging.getLogger("littledoorpost.cycles")

# Sign-ups are advertised in Indian time, and the server runs in UTC. Without
# this the window would open at 05:30 on the 15th for a reader in Delhi. India
# has no daylight saving, so a fixed offset is exact — and saves depending on
# the tz database being installed.
IST = timezone(timedelta(hours=5, minutes=30))

OPEN_DAY = 15   # sign-ups open on the 15th...
CLOSE_DAY = 5   # ...and close on the 5th of the next month


def _month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + by
    return index // 12, index % 12 + 1


def default_window(now: datetime | None = None) -> tuple[str, datetime, datetime]:
    """The cycle we are in or heading towards, and its default dates.

    On the 1st–5th the open window is the one that began on the 15th of last
    month, and it fills *this* month. From the 6th onwards the next window fills
    *next* month — whether it has opened yet (from the 15th) or not (6th–14th).
    """
    now = (now or datetime.now(IST)).astimezone(IST)

    if now.day <= CLOSE_DAY:
        year, month = now.year, now.month
    else:
        year, month = _shift(now.year, now.month, 1)

    open_year, open_month = _shift(year, month, -1)
    opens_at = datetime(open_year, open_month, OPEN_DAY, 0, 0, 0, tzinfo=IST)
    # Inclusive of the whole of the 5th — "closes on the 5th" should mean the
    # end of that day, not one second past midnight.
    closes_at = datetime(year, month, CLOSE_DAY, 23, 59, 59, tzinfo=IST)

    return _month_key(year, month), opens_at, closes_at


async def _ensure(cycle: str, opens_at: datetime, closes_at: datetime) -> dict:
    """Fetch the cycle's row, creating it with the default dates if it is new.

    The conflict clause updates the key to itself: a no-op that still returns
    the existing row, so this is one round trip whether or not it already
    exists — and it never overwrites dates someone has edited.
    """
    return await fetch_one(
        """
        insert into cycles (cycle, opens_at, closes_at)
        values (%s, %s, %s)
        on conflict (cycle) do update set cycle = excluded.cycle
        returning *
        """,
        (cycle, opens_at, closes_at),
    )


async def current(now: datetime | None = None) -> dict:
    """The cycle sign-ups belong to right now, plus whether they are open.

    Looks for a window the clock is actually inside first, so an edited cycle —
    one held open a week longer, say — is honoured over the default rule. Only
    when nothing is open does it fall back to computing the next one.
    """
    now = (now or datetime.now(IST)).astimezone(IST)

    open_now = await fetch_one(
        """
        select * from cycles
        where opens_at <= %s and closes_at >= %s
        order by opens_at desc
        limit 1
        """,
        (now, now),
    )
    if open_now:
        return {**open_now, "open": True}

    cycle, opens_at, closes_at = default_window(now)
    row = await _ensure(cycle, opens_at, closes_at)
    # The row may carry edited dates that leave it shut at this moment.
    return {**row, "open": row["opens_at"] <= now <= row["closes_at"]}


async def roll_over(cycle: str) -> int:
    """Count one delivery against every active subscriber, and retire the spent.

    One statement, so it either applies to everyone or to no one — a connection
    dropped halfway cannot leave half the list decremented.

    It is also idempotent. `last_counted_cycle < cycle` excludes anyone already
    counted for this month, so running it twice is a no-op the second time,
    which is what makes it safe to trigger automatically *and* by hand.

    Returns how many subscriptions it touched.
    """
    rows = await fetch_all(
        """
        update subscribers set
            deliveries_remaining = greatest(deliveries_remaining - 1, 0),
            last_counted_cycle   = %(cycle)s,
            status = case when deliveries_remaining - 1 <= 0 then 'expired' else 'active' end,
            updated_at = now()
        where status = 'active'
          and cycle <= %(cycle)s
          and (last_counted_cycle is null or last_counted_cycle < %(cycle)s)
        returning reference, status, deliveries_remaining
        """,
        {"cycle": cycle},
    )

    if rows:
        done = sum(1 for r in rows if r["status"] == "expired")
        log.info("cycle %s: counted %d subscriptions, %d finished", cycle, len(rows), done)
    return len(rows)


# How long a half-finished sign-up is kept. Long enough that someone who pays
# and then loses their connection can still be reconciled; short enough that
# abandoned attempts never become clutter.
ABANDONED_AFTER = "24 hours"


async def discard_abandoned() -> dict:
    """Clear out sign-ups that were started and never paid for.

    A row has to exist while checkout is in flight — Razorpay wants the order
    created before the customer pays, and if the address lived only in their
    browser, a tab that died mid-payment would leave money taken and nowhere to
    post to. So the row is written first and cleaned up after.

    Two kinds of leftover, handled differently:

    * A **first-timer** who never paid is deleted outright. Nothing is lost.
    * A **returning reader** who abandoned a renewal is *kept* — they have paid
      before, and that ledger is not ours to throw away. Only the abandoned
      attempt goes, and they are put back to 'expired' so they can try again.
    """
    gone = await fetch_all(
        f"""
        delete from subscribers s
        where s.status = 'pending'
          and s.updated_at < now() - interval '{ABANDONED_AFTER}'
          and not exists (select 1 from payments p
                          where p.subscriber_id = s.id and p.status = 'paid')
        returning s.reference
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

    restored = await fetch_all(
        f"""
        update subscribers set status = 'expired', updated_at = now()
        where status = 'pending'
          and updated_at < now() - interval '{ABANDONED_AFTER}'
        returning reference
        """
    )

    if gone or restored:
        log.info(
            "discarded %d abandoned sign-ups, released %d returning readers",
            len(gone), len(restored),
        )
    return {
        "deleted": [r["reference"] for r in gone],
        "released": [r["reference"] for r in restored],
        "attempts_dropped": len(dropped),
    }


async def sweep(now: datetime | None = None) -> list[str]:
    """Roll over every month whose envelopes have had time to go out.

    Timing matters here, and getting it wrong is worse than it looks. A month is
    *not* counted when its window shuts on the 5th — the envelopes have not been
    posted yet at that point, and expiring a one-letter reader early would drop
    them off the mailing list before the letter they paid for was ever sent.

    So a month is counted once the *next* window opens on the 15th, which leaves
    ten days to pack and post.

    This is what makes the lifecycle automatic without a scheduler — Render's
    free plan has no cron, and a sleeping service would miss one anyway. Any
    request that resolves the current cycle passes through here, and because
    roll_over() is idempotent it does not matter how often that happens or how
    many requests arrive at once.
    """
    now = (now or datetime.now(IST)).astimezone(IST)

    # "Has the next window opened?" needs the next window to exist as a row.
    # Resolving the current cycle creates it if it does not.
    await current(now)

    # Same hook, same reasoning: this is the one place every request passes
    # through, so housekeeping rides along rather than needing a scheduler.
    await discard_abandoned()

    due = await fetch_all(
        """
        select c.cycle from cycles c
        where c.rolled_over_at is null
          and c.closes_at < %(now)s
          and exists (select 1 from cycles later
                      where later.cycle > c.cycle and later.opens_at <= %(now)s)
        order by c.cycle
        """,
        {"now": now},
    )

    rolled = []
    for row in due:
        await roll_over(row["cycle"])
        await fetch_one(
            "update cycles set rolled_over_at = now(), updated_at = now() "
            "where cycle = %s and rolled_over_at is null returning cycle",
            (row["cycle"],),
        )
        rolled.append(row["cycle"])
    return rolled
