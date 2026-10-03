"""Make an admin account, or reset its password:  python -m app.create_admin

Run it from backend/, on your own machine, with DATABASE_URL in backend/.env
pointing at the live database. Accounts are made here and only here — there is
no sign-up page for the admin panel, so there is nothing on the web to attack.

Running it again with a username that already exists resets that password and
signs that account out everywhere.
"""

from __future__ import annotations

import getpass
import sys

import psycopg

from .auth import MIN_PASSWORD_LENGTH, hash_password
from .config import get_settings


def ask_password() -> str:
    while True:
        first = getpass.getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters): ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"  Too short - use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if first != getpass.getpass("Same again: "):
            print("  Those did not match. Once more.")
            continue
        return first


def main() -> None:
    settings = get_settings()
    if not settings.dsn:
        print("DATABASE_URL is empty in backend/.env", file=sys.stderr)
        raise SystemExit(1)

    username = (sys.argv[1] if len(sys.argv) > 1 else input("Username: ")).strip()
    if not 3 <= len(username) <= 60:
        print("A username is 3 to 60 characters.", file=sys.stderr)
        raise SystemExit(1)

    hashed = hash_password(ask_password())

    with psycopg.connect(settings.dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id from admin_users where lower(username) = lower(%s)", (username,)
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "update admin_users set password_hash = %s, updated_at = now() where id = %s",
                    (hashed, existing[0]),
                )
                cur.execute("delete from admin_sessions where user_id = %s", (existing[0],))
                print(f"Password reset for {username}. Every browser it was signed in on is signed out.")
            else:
                cur.execute(
                    "insert into admin_users (username, password_hash) values (%s, %s)",
                    (username, hashed),
                )
                print(f"Admin account {username} created. Sign in at /admin on the site.")


if __name__ == "__main__":
    main()
