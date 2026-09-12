"""September's readers, and the rate they keep.

A founding member pays the founding rate however short a plan they take — one
letter costs them what six letters a month costs everybody else.

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

    rate = min(member["rate_minor"], standard_minor)
    log.info("founding rate applied for %s", member["first_name"])
    return rate, member["code"], None
