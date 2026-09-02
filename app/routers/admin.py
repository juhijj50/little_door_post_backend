"""Everything Iris needs to actually post the envelopes.

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

from ..config import current_cycle, get_settings
from ..db import fetch_all, fetch_one
from ..models import AdminStatusIn


async def require_admin(request: Request, token: str | None = Query(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not set on the server.")
    given = request.headers.get("x-admin-token") or token or ""
    # Constant-time: a plain == leaks the token one character at a time.
    if not secrets.compare_digest(given, settings.admin_token):
        raise HTTPException(status_code=401, detail="Not your kingdom.")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

STATUSES = ("pending", "paid", "failed", "cancelled", "waitlist")

LABEL_COLUMNS = [
    "reference", "full_name", "email", "phone", "instagram",
    "address_line1", "address_line2", "landmark", "city", "state", "pincode",
    "country", "birthdate", "interests", "status",
]


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
            count(*) filter (where status = 'paid')     ::int as paid,
            count(*) filter (where status = 'failed')   ::int as failed,
            count(*) filter (where status = 'waitlist') ::int as waitlist
        from subscribers
        """
    )

    return {"cycle": current_cycle(), "counts": counts, "subscriptions": rows}


@router.get("/subscriptions.csv")
async def subscriptions_csv(cycle: str | None = None):
    """Address labels for the month, ready for a mail merge."""
    cycle = cycle or current_cycle()
    rows = await fetch_all(
        f"select {', '.join(LABEL_COLUMNS)} from subscribers "  # noqa: S608 — fixed list above
        "where cycle = %s and status = 'paid' order by full_name",
        (cycle,),
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

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="little-door-post-{cycle}.csv"'},
    )


@router.post("/subscriptions/{subscription_id}/status")
async def set_status(subscription_id: str, body: AdminStatusIn) -> dict:
    row = await fetch_one(
        """
        update subscribers set
            status = %s,
            admin_note = %s,
            paid_at = case when %s = 'paid' then coalesce(paid_at, now()) else null end,
            updated_at = now()
        where id = %s
        returning *
        """,
        (body.status, body.note, body.status, subscription_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")
    return {"subscription": row}


@router.get("/birthdays")
async def birthdays(month: int | None = Query(default=None, ge=1, le=12)) -> dict:
    """The reason the birthdate is worth asking for."""
    month = month or datetime.now().month
    rows = await fetch_all(
        "select reference, full_name, email, birthdate, city, country from subscribers "
        "where birthdate is not null and status = 'paid' "
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
        "where status = 'paid' group by interest order by readers desc, interest"
    )
    notes = await fetch_all(
        "select full_name, interests_note from subscribers "
        "where interests_note is not null and interests_note <> '' "
        "order by created_at desc limit 100"
    )
    return {"tally": tally, "notes": notes}
