"""September's readers, and the rate they keep.

A founding member pays the **twelve-month rate** per month, however short a
plan they take — one letter costs them what a year's subscriber pays a month.
The rate is read live from the India twelve-month row in `plans`, so changing
that price in the admin panel changes theirs with it; `founding_members.rate_minor`
is only the fallback if that row is ever missing.

The code alone is never enough. Codes get screenshotted and passed around, so
`FOUNDING15` only works from the phone number it was issued against: the code
says *which* offer, the number says *whose*. Quoting it from another phone is
refused rather than silently ignored, because somebody who was told they had a
code deserves to know it did not apply.
"""

from __future__ import annotations

import logging

from .db import fetch_one
from .identity import phone_key

log = logging.getLogger("littledoorpost.founding")


async def lookup(phone: str, region: str = "india") -> dict | None:
    """The founding record for this phone number, if there is one."""
    return await fetch_one(
        "select * from founding_members where phone_key = %s",
        (phone_key(phone, region),),
    )


async def rate_for(
    *, phone: str, region: str, code: str | None, standard_minor: int
) -> tuple[int, str | None, str | None]:
    """What one month costs this person, and why.

    Returns ``(rate_minor, applied_code, problem)``. `problem` is a message for
    the reader when they quoted a code that does not hold — never a silent
    downgrade to the standard price, which would charge them more than they
    were expecting without saying so.
    """
    if not code:
        return standard_minor, None, None

    member = await lookup(phone, region)

    if member is None:
        return standard_minor, None, (
            "That code belongs to a different number. "
            "Use the phone you signed up with in September, or clear the code."
        )

    if member["code"] != code:
        return standard_minor, None, "That code is not the one on this number."

    # The twelve-month rate, on or off sale — it is the founding price either
    # way. Only in the reader's own currency: abroad there is no twelve-month
    # plan, and a rupee rate must never be compared with a dollar one.
    year = await fetch_one(
        "select amount_minor from plans where region = %s and months = 12", (region,)
    )
    if year:
        founding_minor = year["amount_minor"]
    elif region == "india":
        founding_minor = member["rate_minor"]
    else:
        founding_minor = standard_minor

    # Never more than the standard price: on the twelve-month plan itself the
    # code simply changes nothing.
    rate = min(founding_minor, standard_minor)
    log.info("founding rate applied for %s", member["first_name"])
    return rate, member["code"], None
