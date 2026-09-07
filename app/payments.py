"""Razorpay, behind a switch.

The account is still being approved, so ``RAZORPAY_KEY_ID`` / ``RAZORPAY_KEY_SECRET``
are empty in .env. While they are:

  * ``payments_available()`` is False,
  * ``create_order()`` is never called,
  * the API tells the site payments are unavailable and it shows a notice.

Filling the two keys in .env and restarting is the whole switch-on — no code
change. Everything below is already the real integration.
"""

from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool

from .config import get_settings

log = logging.getLogger("littledoorpost.payments")

PAYMENTS_PENDING_MESSAGE = (
    "Card and UPI payments are being set up and are not live yet. "
    "Your details are saved — Iris will email you the moment checkout opens."
)


def payments_available() -> bool:
    return get_settings().payments_enabled


def _client():
    import razorpay  # imported here so the app runs without keys configured

    settings = get_settings()
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


async def create_order(*, amount_minor: int, currency: str, reference: str, notes: dict) -> str:
    """Create a Razorpay order and return its id.

    ``amount_minor`` is already in the currency's smallest unit — paise for INR,
    cents for USD — which is exactly what Razorpay expects, so nothing is
    multiplied or rounded on the way in.
    """
    client = _client()
    order = await run_in_threadpool(
        client.order.create,
        {
            "amount": amount_minor,
            "currency": currency,
            "receipt": reference,
            "notes": notes,
            "payment_capture": 1,
        },
    )
    log.info("razorpay order %s created for %s", order["id"], reference)
    return order["id"]


async def verify_signature(*, order_id: str, payment_id: str, signature: str) -> bool:
    """Checkout's success callback is signed — never mark a row paid without
    checking it, or anyone could POST themselves a subscription."""
    client = _client()
    try:
        await run_in_threadpool(
            client.utility.verify_payment_signature,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            },
        )
        return True
    except Exception:  # noqa: BLE001 — the SDK raises SignatureVerificationError
        log.warning("razorpay signature rejected for order %s", order_id)
        return False


async def verify_webhook(*, body: bytes, signature: str) -> bool:
    settings = get_settings()
    if not settings.razorpay_webhook_secret:
        return False
    client = _client()
    try:
        await run_in_threadpool(
            client.utility.verify_webhook_signature,
            body.decode("utf-8"),
            signature,
            settings.razorpay_webhook_secret,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("razorpay webhook signature rejected")
        return False
