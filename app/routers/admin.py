"""The admin panel's API — everything Iris needs to run the club, and no more.

Every route here except /login sits behind a signed-in session (see app/auth.py):
`Authorization: Bearer <token>`, from POST /api/admin/login.

    POST   /login                       sign in
    POST   /logout                      sign out
    POST   /password                    change your password
    GET    /overview                    everything the dashboard shows
    PUT    /edition                     which edition is on sale; open or sold out
    PUT    /editions/{cycle}            what is in that edition's envelope
    GET    /editions/{cycle}/export     that edition's readers, as Excel
    GET    /subscriptions               look a reader up
    POST   /subscriptions/{id}/status   cancel or reinstate a reader
    PUT    /plans/{region}/{months}     change a price
    POST   /media                       upload a photo
    DELETE /media/{id}                  delete a photo
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from psycopg.types.json import Jsonb

from .. import auth, cycles, mail, media, plans, ratelimit
from ..db import fetch_all, fetch_one
from ..export import edition_workbook
from ..models import (
    AdminStatusIn,
    EditionContentsIn,
    EditionIn,
    LoginIn,
    PasswordIn,
    PlanIn,
)

STATUSES = ("pending", "active", "expired", "failed", "cancelled")


# ── signing in ──────────────────────────────────────────────────────────────
# Its own router, because it is the one admin route that cannot require a
# session. Ten tries in fifteen minutes from one address, which is plenty for
# somebody mistyping and nothing for somebody guessing.

public = APIRouter(prefix="/api/admin", tags=["admin"])


@public.post(
    "/login",
    dependencies=[
        Depends(ratelimit.limit(
            "admin-login", times=10, seconds=900,
            message="Too many sign-in attempts. Wait fifteen minutes and try again.",
        ))
    ],
)
async def login(request: Request, body: LoginIn) -> dict:
    return await auth.sign_in(request, body.username, body.password)


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(auth.require_admin)])


@router.post("/logout")
async def logout(request: Request) -> dict:
    await auth.sign_out(request)
    return {"ok": True}


@router.post("/password")
async def change_password(request: Request, body: PasswordIn) -> dict:
    await auth.change_password(request.state.admin, body.current_password, body.new_password)
    return {"ok": True}


# ── the dashboard ───────────────────────────────────────────────────────────

async def _edition_out(row: dict) -> dict:
    return {
        **cycles.describe(row),
        "items": await cycles.contents_for(row),
        "has_own_items": bool(row.get("contents")),
        "envelope_media_id": str(row["envelope_media_id"]) if row.get("envelope_media_id") else None,
        "envelope_image": media.url(row.get("envelope_media_id")),
        "counted": row.get("rolled_over_at") is not None,
    }


@router.get("/overview")
async def overview(request: Request) -> dict:
    await cycles.sweep()
    current = await cycles.current()
    cycle = current["cycle"]

    counts = await fetch_one(
        """
        select
            count(*) filter (where status = 'active' and deliveries_remaining > 0)::int as active,
            count(*)::int as all_time,
            (select count(*)::int from signup_attempts) as unpaid_attempts
        from subscribers
        """
    )
    edition_stats = await fetch_all(
        """
        select cycle, count(*)::int as signups, currency,
               coalesce(sum(amount_minor), 0)::bigint as revenue_minor
        from payments where status = 'paid'
        group by cycle, currency
        """
    )
    by_cycle: dict[str, dict] = {}
    for s in edition_stats:
        entry = by_cycle.setdefault(s["cycle"], {"signups": 0, "revenue": []})
        entry["signups"] += s["signups"]
        entry["revenue"].append(plans.display(s["revenue_minor"], s["currency"]))

    editions = await fetch_all("select * from cycles order by cycle desc limit 24")
    edition_list = []
    for row in editions:
        out = await _edition_out(row)
        stats = by_cycle.get(row["cycle"], {"signups": 0, "revenue": []})
        edition_list.append({**out, **stats, "is_current": row["cycle"] == cycle})

    return {
        "user": {"username": request.state.admin["username"]},
        "edition": await _edition_out(current),
        "editions": edition_list,
        "stats": {
            **counts,
            "signups_this_edition": by_cycle.get(cycle, {}).get("signups", 0),
            "to_post_this_edition": len(await cycles.posting_list(cycle)),
        },
        "plans": [
            {**p, **plans.as_dict(p), "updated_at": p["updated_at"].isoformat()}
            for p in await fetch_all("select * from plans order by region, months")
        ],
        "gallery": await media.gallery(),
        "email": {"transport": mail.transport(), "last": mail.last_result},
    }


# ── editions ────────────────────────────────────────────────────────────────

@router.put("/edition")
async def set_edition(body: EditionIn) -> dict:
    """Put an edition on sale, mark it sold out, or move on to another month.

    Moving forward counts the envelopes of the edition that was on sale — a
    one-month October reader is finished once November is on sale, a
    three-month one has two to go. Safe to repeat: nobody is counted twice for
    one month.
    """
    before = await cycles.current()
    row = await cycles.set_current(body.cycle, body.status)
    counted = {}
    if body.cycle > before["cycle"]:
        counted[before["cycle"]] = await cycles.close_edition(before["cycle"])
    return {"edition": await _edition_out(row), "counted": counted}


@router.put("/editions/{cycle}")
async def set_contents(cycle: str, body: EditionContentsIn) -> dict:
    """What is in one edition's envelope, and its photograph."""
    if not cycles.valid_cycle(cycle):
        raise HTTPException(status_code=422, detail="An edition is written YYYY-MM.")

    image_id = None
    if body.envelope_media_id:
        image_id = media.parse_id(body.envelope_media_id)
        found = await fetch_one(
            "select id from media where id = %s and kind = 'envelope'", (image_id,)
        )
        if not found:
            raise HTTPException(status_code=422, detail="That envelope photo does not exist.")

    previous = await cycles.get(cycle)
    row = await fetch_one(
        """
        insert into cycles (cycle, status, contents, envelope_media_id)
        values (%s, 'open', %s, %s)
        on conflict (cycle) do update set
            contents = excluded.contents,
            envelope_media_id = excluded.envelope_media_id,
            updated_at = now()
        returning *
        """,
        (cycle, Jsonb([i.model_dump() for i in body.items]), image_id),
    )

    # A replaced envelope photo is nobody's any more — clear it out rather than
    # leave it taking up room in the database.
    old = (previous or {}).get("envelope_media_id")
    if old and old != image_id:
        await fetch_one(
            "delete from media where id = %s and kind = 'envelope' "
            "and not exists (select 1 from cycles where envelope_media_id = %s) returning id",
            (old, old),
        )
    return {"edition": await _edition_out(row)}


@router.get("/editions/{cycle}/export")
async def export_edition(cycle: str) -> Response:
    if not cycles.valid_cycle(cycle):
        raise HTTPException(status_code=422, detail="An edition is written YYYY-MM.")
    data = await edition_workbook(cycle)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="little-door-post-{cycle}.xlsx"',
        },
    )


# ── readers ─────────────────────────────────────────────────────────────────

@router.get("/subscriptions")
async def list_subscriptions(
    status: str | None = None,
    cycle: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    status = status if status in STATUSES else None
    cycle = cycle if cycle and cycles.valid_cycle(cycle) else None
    search = f"%{q.strip()}%" if q and q.strip() else None

    rows = await fetch_all(
        """
        select id, reference, full_name, email, phone, instagram, city, country,
               cycle, plan_months, deliveries_remaining, status, paid_at, admin_note
        from subscribers
        where (%(status)s::text is null or status = %(status)s)
          and (%(cycle)s::text is null or cycle = %(cycle)s)
          and (%(q)s::text is null
               or full_name ilike %(q)s or email ilike %(q)s
               or reference ilike %(q)s or phone ilike %(q)s or instagram ilike %(q)s)
        order by created_at desc
        limit %(limit)s
        """,
        {"status": status, "cycle": cycle, "q": search, "limit": limit},
    )
    return {"subscriptions": rows}


@router.post("/subscriptions/{subscription_id}/status")
async def set_status(subscription_id: str, body: AdminStatusIn) -> dict:
    row = await fetch_one(
        """
        update subscribers set
            status = %s,
            admin_note = %s,
            paid_at = case when %s = 'active' then coalesce(paid_at, now()) else paid_at end,
            -- Reactivating by hand restores at least one envelope; anything
            -- else that is not 'active' owes nothing.
            deliveries_remaining = case
                when %s = 'active' then greatest(deliveries_remaining, 1)
                else 0 end,
            updated_at = now()
        where id = %s
        returning id, reference, full_name, status, deliveries_remaining, admin_note
        """,
        (body.status, body.note, body.status, body.status,
         media.parse_id(subscription_id, "No such reader.")),
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such reader.")
    return {"subscription": row}


# ── prices ──────────────────────────────────────────────────────────────────

@router.put("/plans/{region}/{months}")
async def set_plan(region: str, months: int, body: PlanIn) -> dict:
    """Change a price. Takes minor units: 54900 is ₹549, 1300 is $13 — and it
    is the price of ONE month; a longer plan charges it once a month.

    Only new sign-ups see it. Existing rows keep the amount they were charged,
    because a price change must not rewrite what somebody already paid.
    """
    if region not in ("india", "international"):
        raise HTTPException(status_code=422, detail="Region must be india or international.")
    if months not in plans.ALLOWED_MONTHS:
        raise HTTPException(status_code=422, detail="Months must be 1, 3, 6 or 12.")

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
    return {"plan": {**row, **plans.as_dict(row)}}


# ── photographs ─────────────────────────────────────────────────────────────

@router.post("/media")
async def upload(
    request: Request,
    kind: str = Query(default="gallery"),
    caption: str | None = Query(default=None, max_length=200),
) -> dict:
    """The photo is the request body itself — raw bytes, not a form — with
    `kind` (gallery or envelope) and an optional `caption` in the query."""
    if kind not in media.KINDS:
        raise HTTPException(status_code=422, detail="kind must be gallery or envelope.")
    data = await media.read_upload(request)
    row = await media.save(kind, data, (caption or "").strip() or None)
    return {
        "id": str(row["id"]),
        "url": media.url(row["id"]),
        "kind": row["kind"],
        "caption": row["caption"] or "",
        "byte_size": row["byte_size"],
    }


@router.delete("/media/{media_id}")
async def delete_media(media_id: str) -> dict:
    row = await fetch_one(
        "delete from media where id = %s returning id", (media.parse_id(media_id),)
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such photo.")
    return {"deleted": str(row["id"])}
