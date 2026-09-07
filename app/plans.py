"""The price list, read from the database.

Prices live in the `plans` table rather than in .env so they can be changed
without a redeploy, and so a subscriber row can record what was actually
charged at the time — later price changes must not rewrite history.

Amounts are integers in minor units: paise for INR, cents for USD. Money never
touches a float, and Razorpay wants paise regardless.
"""

from __future__ import annotations

from .db import fetch_all, fetch_one

ALLOWED_MONTHS = (1, 3, 6)


def display(amount_minor: int, currency: str) -> str:
    """'27900', 'INR' -> '₹279'. Whole units when it divides evenly, because
    ₹279 reads better than ₹279.00 on a button."""
    symbol = "₹" if currency == "INR" else "$"
    major, minor = divmod(amount_minor, 100)
    return f"{symbol}{major}" if minor == 0 else f"{symbol}{major}.{minor:02d}"


def as_dict(row: dict) -> dict:
    return {
        "months": row["months"],
        "currency": row["currency"],
        "amountMinor": row["amount_minor"],
        "amount": row["amount_minor"] / 100,
        "display": display(row["amount_minor"], row["currency"]),
        # What a month works out at on this plan — the reason to take the longer
        # one, and worth showing next to it.
        "perMonthDisplay": display(round(row["amount_minor"] / row["months"]), row["currency"]),
    }


async def for_region(region: str) -> list[dict]:
    return await fetch_all(
        "select * from plans where region = %s and active order by months",
        (region,),
    )


async def get(region: str, months: int) -> dict | None:
    return await fetch_one(
        "select * from plans where region = %s and months = %s and active",
        (region, months),
    )


async def catalogue() -> dict[str, list[dict]]:
    """Every active plan, grouped by region, for /api/config."""
    rows = await fetch_all("select * from plans where active order by region, months")
    out: dict[str, list[dict]] = {"india": [], "international": []}
    for row in rows:
        out.setdefault(row["region"], []).append(as_dict(row))
    return out
