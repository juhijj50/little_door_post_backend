"""Create or update the tables:  python -m app.migrate

Plain synchronous psycopg — no event loop involved, so this behaves the same on
every platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from .config import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")

# The photographs the gallery started with, copied into the database once so
# they can be deleted from the admin panel like any other. Read from the site's
# folder, which is only there on your own machine — on Render this quietly
# finds nothing and does nothing.
ASSETS = Path(__file__).resolve().parents[2] / "react-app" / "public" / "assets"
STARTING_GALLERY = [
    ("gallery-white-stack.webp", "A stack of white envelopes printed with the blue door and the Little Door Post wordmark"),
    ("gallery-green-seals.webp", "Sage-green envelopes with painted door cards and pressed wax seals"),
    ("gallery-addressed.webp", "Green and white envelopes addressed by hand, stamped with strawberries and little doors"),
    ("gallery-sunlit-nook.webp", "The Sunlit Nook edition laid out: letters, a recipe, a to-do list and stickers"),
    ("gallery-lanterns.webp", "The Land of Lanterns letters, bordered with pumpkins, ghosts and black cats"),
    ("gallery-mayor-prints.webp", "Art prints fanned out in a stack"),
    ("gallery-red-race.webp", "The Red Race envelope opened out: the letter, sticker sheets, wax seals and a colouring page"),
    ("gallery-butterfly-seal.webp", "A white envelope closed with a butterfly wax seal, over a pot of yellow chrysanthemums"),
    ("gallery-packing.webp", "Coloured paper envelopes, handwritten notes, sunflower stickers and sticker books laid out for packing"),
    ("gallery-sketchbook.webp", "A sketchbook open to a drawing of Iris in her yellow cardigan"),
]


def seed_gallery(cur) -> int:
    """Copy the starting photographs in, once. Recorded in data_migrations,
    so deleting them all from the panel later does not bring them back."""
    cur.execute("select 1 from data_migrations where name = 'seed-gallery'")
    if cur.fetchone():
        return 0
    found = [(ASSETS / name, caption) for name, caption in STARTING_GALLERY if (ASSETS / name).exists()]
    if not found:
        return 0
    # Inserted oldest-first in reverse, so the gallery (newest first) shows
    # them in the order above.
    for path, caption in reversed(found):
        data = path.read_bytes()
        cur.execute(
            # clock_timestamp, not now(): now() is the same instant for the
            # whole transaction, and ten equal timestamps have no order.
            "insert into media (kind, content_type, data, byte_size, caption, created_at) "
            "values ('gallery', 'image/webp', %s, %s, %s, clock_timestamp())",
            (data, len(data), caption),
        )
    cur.execute("insert into data_migrations (name) values ('seed-gallery')")
    return len(found)

SETUP_HELP = """
==========================================================================
  Could not reach the database.

  1. Open backend/.env
  2. Paste your Neon connection string into DATABASE_URL
       Neon Console > your project > Connection string > Pooled
  3. Run this again:  python -m app.migrate
==========================================================================
"""


def main() -> None:
    settings = get_settings()
    if not settings.dsn:
        print(SETUP_HELP, file=sys.stderr)
        print("  DATABASE_URL is empty in backend/.env\n", file=sys.stderr)
        raise SystemExit(1)

    try:
        # schema.sql is several statements; psycopg runs them together as long
        # as the call carries no parameters.
        with psycopg.connect(settings.dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA.read_text(encoding="utf-8"))
                seeded = seed_gallery(cur)
    except Exception as exc:
        print(SETUP_HELP, file=sys.stderr)
        print(f"  {exc}\n", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Schema is up to date ({SCHEMA.name}).")
    if seeded:
        print(f"Copied the {seeded} starting gallery photos into the database.")


if __name__ == "__main__":
    main()
