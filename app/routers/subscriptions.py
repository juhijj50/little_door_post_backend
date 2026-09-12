"""Sign-ups, and the payment step that follows one."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import cycles, founding, identity, mail, payments, plans, ratelimit
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
# here so the site and any receipt or email always list the same eight things.
ENVELOPE_CONTENTS = [
    "A letter from Iris about the place she has wandered into",
    "A letter from a side character she met there",
    "A sticker of that month's theme",
    "A sticker of one of the characters",
    "An art print from that month's story",
    "An activity sheet — a puzzle, a recipe, or something to make",
    "A fact sheet on what inspired that month's letter — the real place, "
    "tradition or story behind it",
    "A printed paper stamp of that month's town, for the Wanderland Passport",
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
        promo_code=row.get("promo_code"),
        rate_minor=row.get("rate_minor"),
        rate_display=(
            plans.display(row["rate_minor"], currency)
            if row.get("rate_minor") and currency else None
        ),
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
    """Mark one purchase paid and add the months it bought.

    Both the browser callback and the webhook come through here, so the two can
    never drift apart. `where status = 'pending'` is what makes it safe for both
    to arrive — and for either to be retried — because only the first one to
    land credits anything. Without that guard a reader who paid once could end
    up owed six envelopes for a three-month plan.
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

    # Months are *added*, never assigned. A reader with two envelopes still
    # owed who buys three more ends up owed five; overwriting would quietly
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
        (credited["plan_months"], payment["subscriber_id"]),
    )
    log.info("%s paid for %d month(s)", row["reference"], credited["plan_months"])

    # Told once, when the money actually clears — not when somebody starts a
    # sign-up. Wrapped because the payment is banked and the row is written by
    # now: a dead mailbox must not turn a successful payment into an error for
    # whoever just paid.
    try:
        months = credited["plan_months"]
        amount = plans.display(credited["amount_minor"], credited["currency"])
        rate = plans.display(
            credited["rate_minor"] or credited["amount_minor"] // max(months, 1),
            credited["currency"],
        )
        length = f"{months} month" + ("" if months == 1 else "s")

        address = "\n".join(
            "  " + line
            for line in (
                row["address_line1"],
                row["address_line2"],
                row["landmark"],
                " ".join(filter(None, (row["city"], row["pincode"]))),
                row["state"],
            )
            if line
        )

        # Only shown when there is something to say, so the everyday email
        # stays short enough to read on a phone at the post office.
        extras = ""
        if credited["promo_code"]:
            extras += f"  Code      : {credited['promo_code']} ({rate} a month)\n"
        if row["is_gift"]:
            extras += "\nThis one is a gift. Copy onto the card:\n"
            extras += "\n".join(
                "  " + line for line in row["gift_message"].splitlines()
            ) + "\n"

        await mail.notify(
            f"Paid: {row['full_name']} — {amount}",
            f"{row['full_name']} has paid for {length}"
            f"{'' if months == 1 else f' at {rate} a month'}"
            f" — one envelope each month.\n\n"
            f"  Reference : {row['reference']}\n"
            f"  Amount    : {amount}\n"
            f"  Phone     : {row['phone']}\n"
            f"  Instagram : @{row['instagram'] or '(none given)'}\n"
            f"  Email     : {row['email']}\n"
            f"  Starting  : the {credited['cycle']} envelope\n"
            f"  Owed now  : {row['deliveries_remaining']} envelope"
            f"{'' if row['deliveries_remaining'] == 1 else 's'}\n"
            f"{extras}\n"
            f"Post to:\n{address}\n",
        )
    except Exception:  # noqa: BLE001 — a mail problem is not the payer's
        log.exception("payment credited but could not be emailed: %s", row["reference"])

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
    # Iris posts abroad, but no price is set for it — postage varies too much by
    # country for one figure to cover it. So there is nothing to charge and
    # nothing worth keeping. The site says so and stops; this refuses a direct
    # POST too, so no row can be created by going round the form.
    if body.region == "international":
        raise HTTPException(
            status_code=400,
            detail=(
                "Postage outside India is worked out per country — "
                "write to us on Instagram and Iris will sort it for you."
            ),
        )

    await cycles.sweep()
    cycle = await cycles.current()

    # No window check. The site is only linked from the Instagram bio while
    # sign-ups are running, so being able to reach this at all is the gate —
    # which is also what lets the page render without waiting to be told.
    # `cycle` is still needed: it is the delivery month a purchase buys into,
    # and what the monthly roll-over counts against.

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

    # `plan["amount_minor"]` is the rate for ONE month. A longer plan is a
    # longer commitment at a better monthly rate, not a bundle bought at once —
    # one letter still arrives each month — so the charge is rate x months.
    rate, applied_code, problem = await founding.rate_for(
        phone=body.phone,
        region=body.region,
        code=body.promo_code,
        standard_minor=plan["amount_minor"],
    )
    if problem:
        raise HTTPException(
            status_code=422,
            detail={"error": problem, "fields": {"promo_code": problem}},
        )
    amount = rate * body.plan_months

    # Who this is. First name plus phone, both reduced to a stable key, so the
    # same reader coming back next month lands on the row they already have
    # rather than a fresh one.
    name_key, phone_key = identity.keys(body.first_name, body.phone, body.region)
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
                    f"{owed} envelope{'' if owed == 1 else 's'} still to come. "
                    "Signing up for somebody else in the house? Use their name."
                ),
                # Against the first name, because that is the field the form
                # actually has — hanging it on `full_name` would point at an
                # input that no longer exists, and the reader would see nothing.
                "fields": {"first_name": "This name and number are already subscribed"},
            },
        )

    values = (
        body.full_name, body.first_name, body.last_name, body.email,
        body.phone, body.phone_cc, body.phone_number,
        body.instagram, body.birthdate, body.interests, body.interests_note,
        body.address_line1, body.address_line2, body.landmark, body.city,
        body.state, body.pincode, body.country or "India",
        status, body.plan_months, plan["currency"], amount, rate, applied_code,
        body.is_gift, body.gift_message if body.is_gift else None,
        cycle["cycle"],
    )

    if reader:
        # A returning reader whose last subscription has run out, or one coming
        # back to finish an attempt they abandoned. Same row either way: their
        # details are refreshed — people move — and the counter is left alone,
        # because only a cleared payment adds envelopes.
        row = await fetch_one(
            """
            update subscribers set
                full_name = %s, first_name = %s, last_name = %s, email = %s,
                phone = %s, phone_cc = %s, phone_number = %s,
                instagram = %s, birthdate = %s, interests = %s, interests_note = %s,
                address_line1 = %s, address_line2 = %s, landmark = %s, city = %s,
                state = %s, pincode = %s, country = %s,
                status = %s, plan_months = %s, currency = %s, amount_minor = %s,
                rate_minor = %s, promo_code = %s, is_gift = %s, gift_message = %s,
                cycle = %s, updated_at = now()
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
                full_name, first_name, last_name, email,
                phone, phone_cc, phone_number,
                instagram, birthdate, interests, interests_note,
                address_line1, address_line2, landmark, city,
                state, pincode, country,
                status, plan_months, currency, amount_minor,
                rate_minor, promo_code, is_gift, gift_message,
                cycle, deliveries_remaining
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, 0)
            returning *
            """,
            (make_reference(), body.region, name_key, phone_key, *values),
        )

    # The purchase itself goes in the ledger. One open attempt per reader per
    # month — the unique index sees to that — so retrying a sign-up updates it
    # rather than leaving abandoned rows behind. Paid rows are never touched.
    payment_row = await fetch_one(
        """
        insert into payments (subscriber_id, cycle, plan_months, currency,
                              amount_minor, rate_minor, promo_code)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (subscriber_id, cycle) where status = 'pending'
        do update set
            plan_months = excluded.plan_months,
            currency = excluded.currency,
            amount_minor = excluded.amount_minor,
            -- The rate and the code are kept on the purchase, not just on the
            -- reader, so a later price change or a revoked code never rewrites
            -- what somebody was actually charged.
            rate_minor = excluded.rate_minor,
            promo_code = excluded.promo_code,
            -- A Razorpay order's amount is fixed once created, so a change
            -- of plan has to start a new one.
            razorpay_order_id = case when payments.amount_minor
                                          is distinct from excluded.amount_minor
                                     then null else payments.razorpay_order_id end,
            updated_at = now()
        returning *
        """,
        (row["id"], cycle["cycle"], body.plan_months, plan["currency"],
         amount, rate, applied_code),
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
