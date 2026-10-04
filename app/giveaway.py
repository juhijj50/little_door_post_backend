"""Giveaway codes: one edition's envelope, free.

Each edition can have a giveaway code of its own, set in the admin panel (the
Envelope tab, per month) and kept in `cycles.giveaway_code`. An edition with no
code has no giveaway. The code works only while its edition is the one on sale,
and makes that edition's envelope free: a one-month sign-up costs nothing and
goes straight through without checkout; a longer plan pays for the months after
it (three months costs two).

Unlike a founding code this is not tied to a phone number — anybody who has
the code gets it, which is the point of a giveaway. What limits it is the same
rule as any sign-up (one name and phone number cannot hold two subscriptions at
once), and that the code is whatever was typed into the panel: clear it there
and it stops working at once.
"""

from __future__ import annotations

import re

# What a code may be made of. Codes are compared in capitals with the spaces
# taken out, which is how the sign-up form sends them (models.SubscriberIn).
CODE_RE = re.compile(r"^[A-Z0-9_-]{4,32}$")


def tidy(code: str | None) -> str | None:
    """A code as it is stored and compared: capitals, no spaces; '' is none."""
    code = (code or "").upper().replace(" ", "")
    return code or None


def is_free(code: str | None, edition: dict) -> bool:
    """Whether this code is the giveaway code of the edition on sale."""
    theirs, ours = tidy(code), tidy(edition.get("giveaway_code"))
    return bool(theirs and ours and theirs == ours)
