"""Everything Iris needs to actually run the club.

Guarded by ADMIN_TOKEN, sent as the X-Admin-Token header (or ?token= so a link
can be opened straight in a browser).
"""

from __future__ import annotations

import csv
import io
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import cycles, mail, plans
from ..config import get_settings
from ..db import fetch_all, fetch_one
from ..models import AdminStatusIn, CycleIn, PlanIn


async def require_admin(request: Request, token: str | None = Query(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not set on the server.")
    given = request.headers.get("x-admin-token") or token or ""
    # Constant-time: a plain == leaks the token one character at a time.
    if not secrets.compare_digest(given, settings.admin_token):
        raise HTTPException(status_code=401, detail="Not your kingdom.")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

STATUSES = ("pending", "active", "expired", "failed", "cancelled")

LABEL_COLUMNS = [
    "reference", "full_name", "email", "phone", "instagram",
    "address_line1", "address_line2", "landmark", "city", "state", "pincode",
    "country", "birthdate", "interests", "plan_months", "deliveries_remaining",
]


# ── who to post to ──────────────────────────────────────────────────────────

@router.get("/subscriptions")
async def list_subscriptions(
    status: str | None = None,
    cycle: str | None = None,
    q: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    status = status if status in STATUSES else None
    search = f"%{q.strip()}%" if q and q.strip() else None

    rows = await fetch_all(
        """
        select * from subscribers
        where (%s::text is null or status = %s)
          and (%s::text is null or cycle = %s)
          and (%s::text is null
               or full_name ilike %s or email ilike %s or reference ilike %s)
        order by created_at desc
        limit %s
        """,
        (status, status, cycle, cycle, search, search, search, search, limit),
    )

    counts = await fetch_one(
        """
        select
            count(*) filter (where status = 'pending')  ::int as pending,
            count(*) filter (where status = 'active')   ::int as active,
            count(*) filter (where status = 'expired')  ::int as expired,
            count(*) filter (where status = 'failed')   ::int as failed,
            count(*) filter (where status = 'cancelled')::int as cancelled
        from subscribers
        """
    )

    current = await cycles.current()
    return {"cycle": current["cycle"], "counts": counts, "subscriptions": rows}


@router.get("/mailing-list")
async def mailing_list() -> dict:
    """Everyone owed an envelope right now — the list to actually post to.

    Not filtered by sign-up month: a three-month subscriber from two months ago
    is still owed one, which is the whole point of the counter.

    Swept first, deliberately. This is the list you print labels from, and it is
    read exactly when the site has been quietest — between windows, when no
    visitor has triggered the housekeeping. Without this, a reader whose last
    letter was already counted-but-not-yet-swept would still appear, and get an
    envelope nobody paid for.
    """
    await cycles.sweep()
    rows = await fetch_all(
        "select * from subscribers "
        "where status = 'active' and deliveries_remaining > 0 "
        "order by full_name"
    )
    return {"count": len(rows), "subscribers": rows}


@router.get("/mailing-list.csv")
async def mailing_list_csv():
    """The same list as address labels, ready for a mail merge.

    Swept first for the same reason as `/mailing-list` above.
    """
    await cycles.sweep()
    rows = await fetch_all(
        f"select {', '.join(LABEL_COLUMNS)} from subscribers "  # noqa: S608 — fixed list above
        "where status = 'active' and deliveries_remaining > 0 order by full_name"
    )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=LABEL_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        item = dict(row)
        if item.get("interests"):
            item["interests"] = " | ".join(item["interests"])
        writer.writerow(item)
    buffer.seek(0)

    current = await cycles.current()
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="little-door-post-{current["cycle"]}.csv"'
        },
    )


@router.post("/subscriptions/{subscription_id}/status")
async def set_status(subscription_id: str, body: AdminStatusIn) -> dict:
    row = await fetch_one(
        """
        update subscribers set
            status = %s,
            admin_note = %s,
            paid_at = case when %s = 'active' then coalesce(paid_at, now()) else paid_at end,
            -- Reactivating by hand restores what the plan still owes; anything
            -- else that is not 'active' owes nothing.
            deliveries_remaining = case
                when %s = 'active' then greatest(deliveries_remaining, 1)
                else 0 end,
            updated_at = now()
        where id = %s
        returning *
        """,
        (body.status, body.note, body.status, body.status, subscription_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")
    return {"subscription": row}


@router.get("/payments")
async def list_payments(
    subscriber: str | None = None,
    cycle: str | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """The purchase ledger — every sign-up ever, newest first.

    One row per purchase, so a reader who has come back four times shows four
    rows here and still only one in `subscribers`. This is what a chargeback
    months later is answered with.
    """
    rows = await fetch_all(
        """
        select p.*, s.reference, s.full_name, s.phone, s.email
        from payments p join subscribers s on s.id = p.subscriber_id
        where (%s::uuid is null or p.subscriber_id = %s::uuid)
          and (%s::text is null or p.cycle = %s)
          and (%s::text is null or p.status = %s)
        order by p.created_at desc
        limit %s
        """,
        (subscriber, subscriber, cycle, cycle, status, status, limit),
    )
    totals = await fetch_one(
        """
        select coalesce(sum(amount_minor) filter (where status = 'paid'), 0)::bigint as paid_minor,
               count(*) filter (where status = 'paid')::int    as paid_count,
               count(*) filter (where status = 'pending')::int as pending_count
        from payments
        """
    )
    return {"totals": totals, "payments": rows}


@router.get("/reminders")
async def list_reminders(waiting: bool = True, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """People who asked to be told when sign-ups open.

    `waiting=true` (the default) hides the ones already messaged, so the list is
    a to-do rather than a history.
    """
    rows = await fetch_all(
        """
        select * from reminders
        where (%s = false or notified_at is null)
        order by created_at desc
        limit %s
        """,
        (waiting, limit),
    )
    counts = await fetch_one(
        "select count(*)::int as total, "
        "count(*) filter (where notified_at is null)::int as waiting from reminders"
    )
    return {"counts": counts, "emailSending": mail.email_available(), "reminders": rows}


@router.post("/reminders/{reminder_id}/done")
async def reminder_done(reminder_id: str) -> dict:
    """Mark one as messaged, so it drops off the waiting list."""
    row = await fetch_one(
        "update reminders set notified_at = now() where id = %s returning *", (reminder_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such reminder.")
    return {"reminder": row}


# ── the months themselves ───────────────────────────────────────────────────

@router.get("/cycles")
async def list_cycles(limit: int = Query(default=24, ge=1, le=200)) -> dict:
    await cycles.sweep()
    current = await cycles.current()
    rows = await fetch_all("select * from cycles order by cycle desc limit %s", (limit,))
    return {"current": current["cycle"], "open": current["open"], "cycles": rows}


@router.put("/cycles/{cycle}")
async def set_cycle(cycle: str, body: CycleIn) -> dict:
    """Move one month's window.

    The default is the 15th to the 5th; this overrides it for a single month and
    nothing else. Creating a future month here in advance works too — the row
    simply already exists when that month comes round.
    """
    row = await fetch_one(
        """
        insert into cycles (cycle, opens_at, closes_at, note)
        values (%s, %s, %s, %s)
        on conflict (cycle) do update set
            opens_at = excluded.opens_at,
            closes_at = excluded.closes_at,
            note = excluded.note,
            updated_at = now()
        returning *
        """,
        (cycle, body.opens_at, body.closes_at, body.note),
    )
    return {"cycle": row}


@router.post("/cycles/{cycle}/roll")
async def roll_cycle(cycle: str) -> dict:
    """Count this month's envelopes against everyone: 3 becomes 2, 1 becomes 0
    and expires.

    Normally automatic — any request sweeps months that have closed — so this is
    for posting early, or for re-running after a correction. Safe either way: a
    subscription already counted for this month is skipped, so calling it twice
    changes nothing the second time.
    """
    touched = await cycles.roll_over(cycle)
    await fetch_one(
        "update cycles set rolled_over_at = now(), updated_at = now() "
        "where cycle = %s returning cycle",
        (cycle,),
    )
    return {"cycle": cycle, "counted": touched}


@router.post("/sweep")
async def sweep_now() -> dict:
    """Housekeeping, by hand: roll over any month now due and clear out
    sign-ups that were started and never paid for.

    Both happen on their own as people use the site; this is for forcing it.
    """
    rolled = await cycles.sweep()
    return {"rolled": rolled, "abandoned": await cycles.discard_abandoned()}


# ── prices ──────────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans() -> dict:
    return {"plans": await fetch_all("select * from plans order by region, months")}


@router.put("/plans/{region}/{months}")
async def set_plan(region: str, months: int, body: PlanIn) -> dict:
    """Change a price. Takes minor units: 27900 is ₹279, 1500 is $15.

    Only new sign-ups see it. Existing rows keep the amount they were charged,
    because a price change must not rewrite what somebody already paid.
    """
    if region not in ("india", "international"):
        raise HTTPException(status_code=422, detail="Region must be india or international.")
    if months not in plans.ALLOWED_MONTHS:
        raise HTTPException(status_code=422, detail="Months must be 1, 3 or 6.")

    currency = body.currency or ("INR" if region == "india" else "USD")
    row = await fetch_one(
        """
        insert into plans (region, months, currency, amount_minor, active)
        values (%s, %s, %s, %s, %s)
        on conflict (region, months) do update set
            currency = excluded.currency,
            amount_minor = excluded.amount_minor,
            active = excluded.active,
            updated_at = now()
        returning *
        """,
        (region, months, currency, body.amount_minor, body.active),
    )
    return {"plan": row}


# ── clearing out ────────────────────────────────────────────────────────────

@router.post("/purge")
async def purge(
    days: int = Query(default=365, ge=30, le=3650),
    confirm: bool = Query(default=False),
) -> dict:
    """Permanently delete finished subscriptions older than `days`.

    Expiring a subscription already takes it off every list, so this is only for
    genuinely discarding old records. It is deliberately awkward — it needs
    ?confirm=true, it will not touch anything under 30 days old, and it never
    touches an active or pending row.

    Think before using it: a Razorpay chargeback can arrive months later, and
    the payment record is your evidence. Keeping expired rows costs nothing.
    """
    doomed = await fetch_all(
        "select reference from subscribers "
        "where status in ('expired', 'cancelled', 'failed') "
        "and updated_at < now() - make_interval(days => %s)",
        (days,),
    )
    if not confirm:
        return {
            "would_delete": len(doomed),
            "references": [r["reference"] for r in doomed[:50]],
            "hint": "Add ?confirm=true to actually delete these.",
        }

    deleted = await fetch_all(
        "delete from subscribers "
        "where status in ('expired', 'cancelled', 'failed') "
        "and updated_at < now() - make_interval(days => %s) "
        "returning reference",
        (days,),
    )
    return {"deleted": len(deleted)}


# ── the reason for asking ───────────────────────────────────────────────────

@router.get("/birthdays")
async def birthdays(month: int | None = Query(default=None, ge=1, le=12)) -> dict:
    month = month or datetime.now().month
    rows = await fetch_all(
        "select reference, full_name, email, birthdate, city, country from subscribers "
        "where birthdate is not null and status = 'active' "
        "and extract(month from birthdate) = %s "
        "order by extract(day from birthdate)",
        (month,),
    )
    return {"month": month, "subscribers": rows}


@router.get("/interests")
async def interests() -> dict:
    """What readers say they like — the raw material for next month's letter."""
    tally = await fetch_all(
        "select interest, count(*)::int as readers "
        "from subscribers, unnest(interests) as interest "
        "where status = 'active' group by interest order by readers desc, interest"
    )
    notes = await fetch_all(
        "select full_name, interests_note from subscribers "
        "where interests_note is not null and interests_note <> '' "
        "order by created_at desc limit 100"
    )
    return {"tally": tally, "notes": notes}
