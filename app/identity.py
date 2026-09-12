"""Recognising a returning reader.

A reader is identified by their first name and their phone number. Both have to
be reduced to a stable key first: the same person types their number five
different ways across five months, and none of those strings match each other.

Why first name *and* phone, rather than phone alone — the two mistakes are not
equally bad. Matching on phone alone can merge a parent and child who share a
number into one record, sending an envelope to the wrong address and mixing two
people's payments. Adding the name can instead split one person in two if they
sign up as "Bob" and later as "Robert". A split is a tidy-up; a wrong merge is a
real mess. So the key errs towards splitting.
"""

from __future__ import annotations

import re

# Numbers Indian mobiles are written with: +91 98765 43210, 09876543210,
# 98765-43210 — all the same ten digits underneath.
_NON_DIGITS = re.compile(r"\D")


def phone_key(phone: str, region: str = "india") -> str:
    """Digits only, reduced to the part that actually identifies the line.

        '+91 98765 43210' -> '9876543210'
        '09876543210'     -> '9876543210'
        '98765-43210'     -> '9876543210'
    """
    digits = _NON_DIGITS.sub("", phone or "")

    if region == "india":
        # Strip the country code or a trunk prefix, however it was written.
        if len(digits) > 10 and digits.startswith("91"):
            digits = digits[2:]
        digits = digits.lstrip("0")
        return digits[-10:] if len(digits) >= 10 else digits

    # Abroad the country code is part of the identity, so keep the lot.
    return digits.lstrip("0")


def name_key(name: str) -> str:
    """The first name, folded for comparison.

        'Juhi'             -> 'juhi'
        'Juhi Jani'        -> 'juhi'
        '  juhi  J.'       -> 'juhi'

    Only the first name is ever compared. Surnames are the part people are
    inconsistent about — given, dropped, spelt differently, or in the other
    order — so "Shruti Ranjit Choudhary" and "Shruti Choudhary" have to come out
    the same. Taking the first token means it makes no difference whether the
    first name or the whole name is handed in.
    """
    cleaned = re.sub(r"[^\w\s]", " ", name or "", flags=re.UNICODE)
    parts = cleaned.split()
    return parts[0].casefold() if parts else ""


def keys(name: str, phone: str, region: str = "india") -> tuple[str, str]:
    return name_key(name), phone_key(phone, region)
