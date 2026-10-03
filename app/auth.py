"""Signing in to the admin panel.

A username and password, kept in `admin_users`. The password is never stored:
only an scrypt hash of it, salted per user, and checked in constant time.

Signing in hands the browser a random session token. It is sent back on every
admin request as `Authorization: Bearer <token>`, and only a SHA-256 of it is
kept in `admin_sessions` — so somebody holding a copy of the database holds no
way in. Sessions run out after SESSION_HOURS; signing out, or changing the
password, ends them sooner.

Why a header and not a cookie: the site is on Vercel and the API on Render, two
different domains. A cookie would have to be a third-party cookie to make that
trip, and Safari and Firefox block those by default.

Accounts are made from the command line, never from the web:

    python -m app.create_admin
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .db import fetch_one
from .ratelimit import client_ip

log = logging.getLogger("littledoorpost.auth")

SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 10

# scrypt at the cost OWASP recommends: about 50 ms and 16 MB a guess, which is
# nothing for one sign-in and a great deal for somebody trying millions.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """'scrypt$16384$8$1$<salt>$<hash>' — the parameters travel with the hash,
    so they can be raised later without breaking the passwords already set."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against when the username does not exist, so a wrong username takes
# as long to refuse as a wrong password. Otherwise the timing alone would say
# which usernames are real.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def sign_in(request: Request, username: str, password: str) -> dict:
    """Check a username and password; on success, open a session.

    Every failure gets the same answer, whether the username or the password
    was wrong — telling them apart would let anyone list the usernames.
    """
    user = await fetch_one(
        "select * from admin_users where lower(username) = lower(%s)", (username,)
    )
    ok = await run_in_threadpool(
        verify_password, password, user["password_hash"] if user else _DUMMY_HASH
    )
    if not user or not ok:
        log.warning("admin sign-in refused for %r from %s", username, client_ip(request))
        raise HTTPException(status_code=401, detail="That username and password do not match.")

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    await fetch_one(
        "insert into admin_sessions (token_hash, user_id, expires_at, ip) "
        "values (%s, %s, %s, %s) returning token_hash",
        (_token_hash(token), user["id"], expires, client_ip(request)),
    )
    await fetch_one(
        "update admin_users set last_login_at = now() where id = %s returning id", (user["id"],)
    )
    log.info("admin %s signed in from %s", user["username"], client_ip(request))
    return {"token": token, "expires_at": expires.isoformat(), "username": user["username"]}


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def require_admin(request: Request) -> dict:
    """The dependency every admin route sits behind. Returns the signed-in
    user, and stores it on the request for the routes that need it."""
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in.")

    user = await fetch_one(
        """
        select u.id, u.username, s.token_hash
        from admin_sessions s join admin_users u on u.id = s.user_id
        where s.token_hash = %s and s.expires_at > now()
        """,
        (_token_hash(token),),
    )
    if not user:
        raise HTTPException(status_code=401, detail="Your session has ended. Please sign in again.")
    request.state.admin = user
    return user


async def sign_out(request: Request) -> None:
    token = _bearer(request)
    if token:
        await fetch_one(
            "delete from admin_sessions where token_hash = %s returning token_hash",
            (_token_hash(token),),
        )


async def change_password(user: dict, current: str, new: str) -> None:
    """Change the signed-in user's password, and sign out every other browser."""
    row = await fetch_one("select * from admin_users where id = %s", (user["id"],))
    ok = await run_in_threadpool(verify_password, current, row["password_hash"])
    if not ok:
        raise HTTPException(
            status_code=422,
            detail={"error": "Your current password is not right.",
                    "fields": {"current_password": "Not right"}},
        )
    hashed = await run_in_threadpool(hash_password, new)
    await fetch_one(
        "update admin_users set password_hash = %s, updated_at = now() where id = %s returning id",
        (hashed, user["id"]),
    )
    await fetch_one(
        "delete from admin_sessions where user_id = %s and token_hash <> %s returning user_id",
        (user["id"], user["token_hash"]),
    )
    log.info("admin %s changed their password", user["username"])
