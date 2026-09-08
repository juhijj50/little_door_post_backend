"""Sign-ups, and the payment step that follows one."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import cycles, identity, mail, payments, plans, ratelimit
from ..config import get_settings, make_reference
from ..db import fetch_one
from ..models import (
    PaymentOut,
    ReminderIn,
    RazorpayVerifyIn,
    SubscribeResponse,
    SubscriberIn,
    SubscriptionOut,
)

log = logging.getLogger("littledoorpost.subscriptions")

router = APIRouter(prefix="/api", tags=["subscriptions"])

# Everything a reader gets, in the order it sits in the envelope. Served from
# here so the site and any receipt or email always list the same six things.
ENVELOPE_CONTENTS = [
    "A letter from Iris about the place she has wandered into",
    "A letter from a side character she met there",
    "A sticker of that month's theme",
    "A sticker of one of the characters",
    "An art print from that month's story",
    "A fun activity or fact sheet",
]


def _out(row: dict) -> SubscriptionOut:
    amount, currency = row.get("amount_minor"), row.get("currency")
    return SubscriptionOut(
        id=str(row["id"]),
        reference=row["reference"],
        region=row["region"],
        status=row["status"],
        full_name=row["full_name"],
        cycle=row["cycle"],
        plan_months=row.get("plan_months") or 1,
        currency=currency,
        amount_minor=amount,
        amount_display=plans.display(amount, currency) if amount and currency else None,
        deliveries_remaining=row.get("deliveries_remaining") or 0,
    )


async def _open_payment(subscriber_id) -> dict | None:
    """The unpaid purchase a reader is partway through, if any."""
    return await fetch_one(
        "select * from payments where subscriber_id = %s and status = 'pending' "
        "order by created_at desc limit 1",
        (subscriber_id,),
    )


async def _payment_for(row: dict, payment: dict | None = None) -> PaymentOut:
    """Build the checkout payload for an unpaid purchase, opening a Razorpay
    order if the keys are in place. Without keys this is the notice the form
    shows instead."""
    settings = get_settings()
    payment = payment or await _open_payment(row["id"])
    if payment is None:
        # Nothing outstanding — they are paid up.
        return PaymentOut(enabled=False, message="This subscription is already paid for.")

    amount, currency = payment["amount_minor"], payment["currency"]
    shown = plans.display(amount, currency)

    if not payments.payments_available():
        return PaymentOut(
            enabled=False,
            amount_minor=amount,
            amount_display=shown,
            currency=currency,
            message=payments.PAYMENTS_PENDING_MESSAGE,
        )

    order_id = payment.get("razorpay_order_id")
    if not order_id:
        order_id = await payments.create_order(
            amount_minor=amount,
            currency=currency,
            reference=row["reference"],
            notes={
                "reference": row["reference"],
                "cycle": payment["cycle"],
                "months": str(payment["plan_months"]),
            },
        )
        await fetch_one(
            "update payments set razorpay_order_id = %s, updated_at = now() "
            "where id = %s returning id",
            (order_id, payment["id"]),
        )

    return PaymentOut(
        enabled=True,
        amount_minor=amount,
        amount_display=shown,
        currency=currency,
        key_id=settings.razorpay_key_id,
        order_id=order_id,
    )


async def _credit(payment: dict, razorpay_payment_id: str | None) -> dict:
    """Mark one purchase paid and add the letters it bought.

    Both the browser callback and the webhook come through here, so the two can
    never drift apart. `where status = 'pending'` is what makes it safe for both
    to arrive — and for either to be retried — because only the first one to
    land credits anything. Without that guard a reader who paid once could end
    up owed six letters for a three-letter plan.
    """
    credited = await fetch_one(
        """
        update payments set
            status = 'paid',
            razorpay_payment_id = coalesce(%s, razorpay_payment_id),
            paid_at = coalesce(paid_at, now()),
            updated_at = now()
        where id = %s and status = 'pending'
        returning *
        """,
        (razorpay_payment_id, payment["id"]),
    )

    if not credited:
        return await fetch_one(
            "select * from subscribers where id = %s", (payment["subscriber_id"],)
        )

    # Letters are *added*, never assigned. A reader with two still owed who buys
    # three more ends up owed five; overwriting would quietly swallow the two
    # they had already paid for.
    row = await fetch_one(
        """
        update subscribers set
            status = 'active',
            deliveries_remaining = deliveries_remaining + %s,
            paid_at = coalesce(paid_at, now()),
            updated_at = now()
        where id = %s
        returning *
        """,
        (credited["plan_months"], payment["subscriber_id"]),
    )
    log.info("%s paid for %d letters", row["reference"], credited["plan_months"])
    return row


@router.get("/config")
async def config() -> dict:
    """Prices, the current window and payment availability, read at page load.

    Resolving the cycle also sweeps any month that has closed since the last
    request, which is what advances everyone's delivery counter without a cron.
    """
    await cycles.sweep()
    cycle = await cycles.current()

    return {
        "signupOpen": cycle["open"],
        "cycle": cycle["cycle"],
        "opensAt": cycle["opens_at"].isoformat(),
        "closesAt": cycle["closes_at"].isoformat(),
        "plans": await plans.catalogue(),
        "paymentsEnabled": payments.payments_available(),
        "paymentsMessage": None if payments.payments_available() else payments.PAYMENTS_PENDING_MESSAGE,
        "internationalOpen": False,
        "contents": ENVELOPE_CONTENTS,
    }


@router.post(
    "/subscriptions",
    response_model=SubscribeResponse,
    status_code=201,
    dependencies=[
        Depends(ratelimit.limit(
            "signups", times=20, seconds=3600,
            message="Too many sign-up attempts from here. Try again in an hour.",
        ))
    ],
)
async def create_subscription(body: SubscriberIn) -> SubscribeResponse:
    # Nothing is posted outside India yet, so there is nothing to sell and
    # nothing worth keeping. The site says so and stops; this refuses a direct
    # POST too, so no row can be created by going round the form.
    if body.region == "international":
        raise HTTPException(
            status_code=400,
            detail="The Little Door Post only ships within India at the moment.",
        )

    await cycles.sweep()
    cycle = await cycles.current()

    if not cycle["open"]:
        raise HTTPException(
            status_code=409,
            detail="Sign-ups are closed just now. The next window opens on the 15th.",
        )

    plan = await plans.get(body.region, body.plan_months)
    if not plan:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "That subscription length is not available.",
                "fields": {"plan_months": "Choose 1, 3 or 6 months"},
            },
        )

    status = "pending"
    amount = plan["amount_minor"]

    # Who this is. First name plus phone, both reduced to a stable key, so the
    # same reader coming back next month lands on the row they already have
    # rather than a fresh one.
    name_key, phone_key = identity.keys(body.full_name, body.phone, body.region)
    reader = await fetch_one(
        "select * from subscribers where name_key = %s and phone_key = %s",
        (name_key, phone_key),
    )

    # Already have envelopes coming? Then this is a duplicate, not a renewal.
    # Refused rather than merged, because the alternative is quietly overwriting
    # one person's address with another's when a household shares a phone.
    if reader and reader["deliveries_remaining"] > 0:
        owed = reader["deliveries_remaining"]
        raise HTTPException(
            status_code=409,
            detail={
                "error": (
                    f"{reader['full_name']} is already subscribed on this number — "
                    f"{owed} letter{'' if owed == 1 else 's'} still to come. "
                    "Signing up for somebody else in the house? Use their name."
                ),
                "fields": {"full_name": "This name and number are already subscribed"},
            },
        )

    values = (
        body.full_name, body.email, body.phone, body.instagram, body.birthdate,
        body.interests, body.interests_note, body.address_line1, body.address_line2,
        body.landmark, body.city, body.state, body.pincode, body.country or "India",
        status, body.plan_months, plan["currency"], amount, cycle["cycle"],
    )

    if reader:
        # A returning reader whose last subscription has run out, or one coming
        # back to finish an attempt they abandoned. Same row either way: their
        # details are refreshed — people move — and the counter is left alone,
        # because only a cleared payment adds envelopes.
        row = await fetch_one(
            """
            update subscribers set
                full_name = %s, email = %s, phone = %s, instagram = %s, birthdate = %s,
                interests = %s, interests_note = %s, address_line1 = %s,
                address_line2 = %s, landmark = %s, city = %s, state = %s,
                pincode = %s, country = %s, status = %s, plan_months = %s,
                currency = %s, amount_minor = %s, cycle = %s,
                updated_at = now()
            where id = %s
            returning *
            """,
            (*values, reader["id"]),
        )
    else:
        row = await fetch_one(
            """
            insert into subscribers (
                reference, region, name_key, phone_key,
                full_name, email, phone, instagram, birthdate, interests,
                interests_note, address_line1, address_line2, landmark, city,
                state, pincode, country, status, plan_months, currency,
                amount_minor, cycle, deliveries_remaining
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, 0)
            returning *
            """,
            (make_reference(), body.region, name_key, phone_key, *values),
        )

    # The purchase itself goes in the ledger. One open attempt per reader per
    # month — the unique index sees to that — so retrying a sign-up updates it
    # rather than leaving abandoned rows behind. Paid rows are never touched.
    payment_row = await fetch_one(
        """
        insert into payments (subscriber_id, cycle, plan_months, currency, amount_minor)
        values (%s, %s, %s, %s, %s)
        on conflict (subscriber_id, cycle) where status = 'pending'
        do update set
            plan_months = excluded.plan_months,
            currency = excluded.currency,
            amount_minor = excluded.amount_minor,
            -- A Razorpay order's amount is fixed once created, so a change
            -- of plan has to start a new one.
            razorpay_order_id = case when payments.amount_minor
                                          is distinct from excluded.amount_minor
                                     then null else payments.razorpay_order_id end,
            updated_at = now()
        returning *
        """,
        (row["id"], cycle["cycle"], body.plan_months, plan["currency"], amount),
    )

    return SubscribeResponse(
        subscription=_out(row),
        payment=await _payment_for(row, payment_row),
    )


@router.get("/subscriptions/{subscription_id}", response_model=SubscribeResponse)
async def get_subscription(subscription_id: str) -> SubscribeResponse:
    row = await fetch_one("select * from subscribers where id = %s", (subscription_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")
    return SubscribeResponse(
        subscription=_out(row),
        payment=await _payment_for(row),
    )


@router.post("/subscriptions/{subscription_id}/order", response_model=PaymentOut)
async def create_order(subscription_id: str) -> PaymentOut:
    """Re-open checkout for a sign-up that was left unpaid."""
    row = await fetch_one("select * from subscribers where id = %s", (subscription_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")
    if row["status"] in ("active", "expired"):
        raise HTTPException(status_code=409, detail="This sign-up is already paid for.")
    return await _payment_for(row)


@router.post("/subscriptions/{subscription_id}/verify", response_model=SubscribeResponse)
async def verify_payment(subscription_id: str, body: RazorpayVerifyIn) -> SubscribeResponse:
    """Called by the site when Razorpay Checkout reports success.

    The signature is what makes this safe: without checking it, anyone could
    POST arbitrary ids here and mark themselves paid.
    """
    if not payments.payments_available():
        raise HTTPException(status_code=503, detail=payments.PAYMENTS_PENDING_MESSAGE)

    row = await fetch_one("select * from subscribers where id = %s", (subscription_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")

    payment = await fetch_one(
        "select * from payments where subscriber_id = %s and razorpay_order_id = %s",
        (row["id"], body.razorpay_order_id),
    )
    if not payment:
        raise HTTPException(status_code=400, detail="That payment belongs to a different sign-up.")

    ok = await payments.verify_signature(
        order_id=body.razorpay_order_id,
        payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="We could not verify that payment. Nothing has been charged twice — "
            "please try again or write to us.",
        )

    row = await _credit(payment, body.razorpay_payment_id)

    return SubscribeResponse(subscription=_out(row), payment=None)


# ── Razorpay webhook ────────────────────────────────────────────────────────
#
# The browser callback above is the normal path, but it only runs if the
# reader's tab survives long enough to fire it. Someone who pays and then closes
# the window, or loses signal on the way back, would otherwise be charged while
# their row sat unpaid. Razorpay reports the same payment here, server to
# server, so the purchase lands either way.
#
# Razorpay Dashboard > Settings > Webhooks
#   URL     https://<your-service>.onrender.com/api/payments/webhook
#   Secret  a string you invent, also set as RAZORPAY_WEBHOOK_SECRET
#   Events  payment.captured, payment.failed

# Razorpay retries anything that is not 2xx, and disables a webhook that keeps
# failing. So: verify, do what can be done, and answer 200 regardless — an event
# about an order we do not recognise is not worth retrying for a week.
_ACK = {"status": "ok"}


@router.post("/payments/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request) -> dict:
    # The signature covers the exact bytes Razorpay sent, so this has to be the
    # raw body — parsing first and re-serialising would change it.
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not await payments.verify_webhook(body=raw, signature=signature):
        # 403 and not 200: a bad signature means this did not come from
        # Razorpay, or RAZORPAY_WEBHOOK_SECRET does not match the dashboard.
        # Worth surfacing rather than silently accepting.
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("razorpay webhook: body was not JSON")
        return _ACK

    kind = event.get("event", "")
    entity = (event.get("payload", {}).get("payment", {}) or {}).get("entity", {}) or {}
    order_id = entity.get("order_id")

    if kind not in ("payment.captured", "payment.failed") or not order_id:
        log.info("razorpay webhook: ignoring %s", kind or "(no event name)")
        return _ACK

    payment = await fetch_one(
        "select * from payments where razorpay_order_id = %s", (order_id,)
    )
    if not payment:
        # An order from another system, or one whose sign-up was discarded as
        # abandoned. Nothing to do, and nothing Razorpay should retry.
        log.info("razorpay webhook: %s for unknown order %s", kind, order_id)
        return _ACK

    if kind == "payment.captured":
        await _credit(payment, entity.get("id"))
    else:
        # Only an attempt still open is marked failed — a later failure event
        # must never undo a payment that has already been credited.
        await fetch_one(
            """
            update payments set status = 'failed', updated_at = now()
            where id = %s and status = 'pending'
            returning id
            """,
            (payment["id"],),
        )
        log.info("razorpay webhook: payment failed for order %s", order_id)

    return _ACK


# ── reminders ───────────────────────────────────────────────────────────────

@router.post(
    "/reminders",
    status_code=201,
    dependencies=[
        Depends(ratelimit.limit(
            "reminders", times=3, seconds=3600,
            message="That is enough reminders for now. Try again in an hour.",
        ))
    ],
)
async def create_reminder(body: ReminderIn) -> dict:
    """"Tell me when sign-ups open."

    Only offered while the window is shut — when it is open there is a form to
    fill in instead, and a reminder would be a strange thing to ask for.

    Asking twice is not an error. The unique index quietly keeps the first
    request, so a reader who taps the button again gets the same friendly answer
    rather than a complaint, and the inbox gets one message rather than five.
    """
    cycle = await cycles.current()

    row = await fetch_one(
        """
        insert into reminders (instagram, instagram_key, email, cycle)
        values (%s, %s, %s, %s)
        on conflict (instagram_key, cycle) do update set instagram = excluded.instagram
        returning *, (xmax = 0) as is_new
        """,
        (body.instagram, body.instagram.casefold(), body.email, cycle["cycle"]),
    )

    if row["is_new"]:
        # The row is saved by this point, and it is the part that matters — the
        # waiting list can be read in the admin whether or not the mail goes
        # anywhere. So nothing about emailing is allowed to turn a request that
        # already succeeded into an error for the reader.
        try:
            opens = cycle["opens_at"].astimezone(cycles.IST).strftime("%d %B %Y")
            # "October reminder — @handle": the month the envelope goes out, so
            # a season of these threads together in the inbox.
            month = datetime.strptime(cycle["cycle"], "%Y-%m").strftime("%B")
            await mail.notify(
                f"{month} reminder — @{body.instagram}",
                f"@{body.instagram} asked to be told when sign-ups open.\n\n"
                f"  Instagram : @{body.instagram}\n"
                f"  Waiting for: the {cycle['cycle']} envelope, window opens {opens}\n\n"
                f"Message them on Instagram when it does.\n",
            )
        except Exception:  # noqa: BLE001 — a mail problem is not the reader's
            log.exception("reminder saved but could not be emailed: @%s", body.instagram)

    return {
        "ok": True,
        "instagram": row["instagram"],
        "cycle": row["cycle"],
        "already_asked": not row["is_new"],
    }
