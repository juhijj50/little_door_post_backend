"""Sign-ups, and the payment step that follows one."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from .. import payments
from ..config import current_cycle, get_settings, make_reference, signup_open
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
    return SubscriptionOut(
        id=str(row["id"]),
        reference=row["reference"],
        region=row["region"],
        status=row["status"],
        full_name=row["full_name"],
        amount_inr=row["amount_inr"],
        cycle=row["cycle"],
    )


async def _payment_for(row: dict) -> PaymentOut:
    """Build the checkout payload, opening a Razorpay order if the keys are in
    place. Without keys this is the notice the form shows instead."""
    settings = get_settings()
    if not payments.payments_available():
        return PaymentOut(
            enabled=False,
            amount_inr=row["amount_inr"],
            message=payments.PAYMENTS_PENDING_MESSAGE,
        )

    order_id = row.get("razorpay_order_id")
    if not order_id:
        order_id = await payments.create_order(
            amount_inr=row["amount_inr"],
            reference=row["reference"],
            notes={"reference": row["reference"], "cycle": row["cycle"]},
        )
        await fetch_one(
            "update subscribers set razorpay_order_id = %s, updated_at = now() "
            "where id = %s returning id",
            (order_id, row["id"]),
        )

    return PaymentOut(
        enabled=True,
        amount_inr=row["amount_inr"],
        key_id=settings.razorpay_key_id,
        order_id=order_id,
    )


@router.get("/config")
async def config() -> dict:
    """Price, window and payment availability, read at page load so none of it
    is baked into the JavaScript bundle."""
    settings = get_settings()
    return {
        "signupOpen": signup_open(),
        "cycle": current_cycle(),
        "priceInr": settings.subscription_price_inr,
        "currency": "INR",
        "paymentsEnabled": payments.payments_available(),
        "paymentsMessage": None if payments.payments_available() else payments.PAYMENTS_PENDING_MESSAGE,
        "internationalOpen": False,
        "contents": ENVELOPE_CONTENTS,
    }


@router.post("/subscriptions", response_model=SubscribeResponse, status_code=201)
async def create_subscription(body: SubscriberIn) -> SubscribeResponse:
    settings = get_settings()
    cycle = current_cycle()
    international = body.region == "international"

    if not international and not signup_open():
        raise HTTPException(
            status_code=409,
            detail="Sign-ups are closed for this month. The next window opens on the 20th.",
        )

    # There is no post outside India yet, so there is nothing to charge for.
    status = "waitlist" if international else "pending"
    amount = None if international else settings.subscription_price_inr

    existing = await fetch_one(
        "select * from subscribers "
        "where lower(email) = lower(%s) and cycle = %s and region = %s "
        "order by created_at desc limit 1",
        (body.email, cycle, body.region),
    )

    if existing and existing["status"] == "paid":
        raise HTTPException(
            status_code=409,
            detail="This email is already on this month's post. Iris has you.",
        )

    values = (
        body.full_name, body.phone, body.instagram, body.birthdate, body.interests,
        body.interests_note, body.address_line1, body.address_line2, body.landmark,
        body.city, body.state, body.pincode, body.country or "India", status, amount,
    )

    if existing:
        # A reader coming back to finish this month's sign-up — overwrite the
        # row they left behind rather than piling up duplicates.
        row = await fetch_one(
            """
            update subscribers set
                full_name = %s, phone = %s, instagram = %s, birthdate = %s,
                interests = %s, interests_note = %s, address_line1 = %s,
                address_line2 = %s, landmark = %s, city = %s, state = %s,
                pincode = %s, country = %s, status = %s, amount_inr = %s,
                updated_at = now()
            where id = %s
            returning *
            """,
            (*values, existing["id"]),
        )
    else:
        row = await fetch_one(
            """
            insert into subscribers (
                reference, region, cycle, email,
                full_name, phone, instagram, birthdate, interests, interests_note,
                address_line1, address_line2, landmark, city, state, pincode,
                country, status, amount_inr
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning *
            """,
            (make_reference(), body.region, cycle, body.email, *values),
        )

    return SubscribeResponse(
        subscription=_out(row),
        payment=None if international else await _payment_for(row),
    )


@router.get("/subscriptions/{subscription_id}", response_model=SubscribeResponse)
async def get_subscription(subscription_id: str) -> SubscribeResponse:
    row = await fetch_one("select * from subscribers where id = %s", (subscription_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such sign-up.")
    return SubscribeResponse(
        subscription=_out(row),
        payment=None if row["region"] == "international" else await _payment_for(row),
    )


@router.post("/subscriptions/{subscription_id}/order", response_model=PaymentOut)
async def create_order(subscription_id: str) -> PaymentOut:
    """Re-open checkout for a sign-up that was left unpaid."""
    row = await fetch_one("select * from subscribers where id = %s", (subscription_id,))
    if not row or row["region"] != "india":
        raise HTTPException(status_code=404, detail="No such sign-up.")
    if row["status"] == "paid":
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
    if row["razorpay_order_id"] != body.razorpay_order_id:
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

    updated = await fetch_one(
        "update subscribers set status = 'paid', razorpay_payment_id = %s, "
        "paid_at = coalesce(paid_at, now()), updated_at = now() "
        "where id = %s returning *",
        (body.razorpay_payment_id, row["id"]),
    )
    log.info("subscription %s paid", updated["reference"])
    return SubscribeResponse(subscription=_out(updated), payment=None)
