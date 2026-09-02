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
    except Exception as exc:
        print(SETUP_HELP, file=sys.stderr)
        print(f"  {exc}\n", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Schema is up to date ({SCHEMA.name}).")


if __name__ == "__main__":
    main()
