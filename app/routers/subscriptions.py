"""Sign-ups, and the payment step that follows one."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from .. import cycles, identity, payments, plans
from ..config import get_settings, make_reference
from ..db import fetch_one
from ..models import (
    PaymentOut,
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


@router.post("/subscriptions", response_model=SubscribeResponse, status_code=201)
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

    # `where status = 'pending'` is the guard that makes this idempotent: the
    # browser callback, a retry of it and the webhook can all arrive, and only
    # the first one credits the purchase.
    credited = await fetch_one(
        """
        update payments set
            status = 'paid', razorpay_payment_id = %s,
            paid_at = coalesce(paid_at, now()), updated_at = now()
        where id = %s and status = 'pending'
        returning *
        """,
        (body.razorpay_payment_id, payment["id"]),
    )

    if credited:
        # Envelopes are *added*, never assigned. A reader with two still owed
        # who buys three more ends up owed five — overwriting would quietly
        # swallow the two they had already paid for.
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
            (credited["plan_months"], row["id"]),
        )
        log.info("%s paid for %d letters", row["reference"], credited["plan_months"])
    else:
        row = await fetch_one("select * from subscribers where id = %s", (row["id"],))

    return SubscribeResponse(subscription=_out(row), payment=None)
