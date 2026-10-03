"""The site's colour palettes, chosen in the admin panel's Design tab.

Two choices, each from the same list: the palette of the buttons (and the
highlights that go with them — selected plans, borders, links) and the colour
of the headings. Only the names live here; the colours themselves are in
react-app/src/theme.js, which turns each name into a full set of shades.
Keep the two lists in step.
"""

from __future__ import annotations

from .db import fetch_all, fetch_one

PALETTES = (
    "sage", "forest", "teal", "periwinkle", "navy",
    "plum", "rose", "terracotta", "ochre", "cocoa", "charcoal",
)
DEFAULT = "sage"
KEYS = ("buttons", "headings")


async def theme() -> dict[str, str]:
    rows = await fetch_all("select key, value from site_settings where key = any(%s)", (list(KEYS),))
    chosen = {r["key"]: r["value"] for r in rows}
    return {k: chosen.get(k) if chosen.get(k) in PALETTES else DEFAULT for k in KEYS}


async def set_value(key: str, value: str) -> None:
    await fetch_one(
        """
        insert into site_settings (key, value) values (%s, %s)
        on conflict (key) do update set value = excluded.value, updated_at = now()
        returning key
        """,
        (key, value),
    )
