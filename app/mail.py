"""Sending the email this club needs: "somebody has paid", "somebody abroad
wants the post".

Two ways out, tried in this order:

  1. Brevo's HTTP API, when BREVO_API_KEY is set.
  2. SMTP (Gmail, by default), when EMAIL_USER / EMAIL_PASSWORD are set.

Why two. Render's free tier has blocked outbound SMTP — ports 25, 465 and 587 —
since September 2025. On a free instance the SMTP path cannot work however right
the password is: the connection is refused before Gmail ever sees it. HTTPS is
not blocked, so an HTTP email API is the way out that does not cost a paid
instance. SMTP stays as the fallback because it is what works locally and on a
paid instance, with nothing to sign up for.

Both use the standard library, so there is still no extra dependency.

A send that fails never breaks the request that caused it — by the time this
runs, the payment or the sign-up is already safely in the database. But it is
no longer silent about it either. The first version logged a failure as a
warning and moved on, which is exactly how paid orders went un-emailed with
nobody noticing. Failures are now logged as errors and remembered, and
/api/admin/test-email reports them.
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

from starlette.concurrency import run_in_threadpool

from .config import get_settings

log = logging.getLogger("littledoorpost.mail")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"

# The most recent outcome, so a problem can be seen without reading server
# logs. In memory only: it resets on restart, which is fine — it answers "is
# email working right now", not "has it ever worked".
last_result: dict = {"ok": None, "transport": None, "error": None, "at": None, "subject": None}


def transport() -> str | None:
    """Which way a send would go, or None if nothing is configured."""
    settings = get_settings()
    if settings.brevo_api_key and settings.notify_to:
        return "brevo"
    if settings.email_enabled:
        return "smtp"
    return None


def email_available() -> bool:
    return transport() is not None


def _send_brevo(subject: str, body: str, to: str | None = None) -> None:
    """Blocking send over HTTPS. Called through a worker thread."""
    settings = get_settings()
    payload = {
        "sender": {"email": settings.notify_from, "name": "The Little Door Post"},
        "to": [{"email": to or settings.notify_to}],
        "subject": subject,
        "textContent": body,
    }
    request = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": settings.brevo_api_key,
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 300:
                raise RuntimeError(f"Brevo answered {response.status}")
    except urllib.error.HTTPError as exc:
        # Brevo explains itself in the body — an unverified sender, a bad key —
        # and that sentence is the whole diagnosis, so keep it.
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Brevo answered {exc.code}: {detail}") from None


def _send_smtp(subject: str, body: str, to: str | None = None) -> None:
    """Blocking send over SMTP. Called through a worker thread — SMTP is slow
    and would otherwise hold up the request that triggered it."""
    settings = get_settings()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.email_user
    message["To"] = to or settings.email_to or settings.email_user
    message.set_content(body)

    with smtplib.SMTP(settings.email_host, settings.email_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.email_user, settings.email_password)
        smtp.send_message(message)


def _record(ok: bool, how: str | None, subject: str, error: str | None = None) -> None:
    last_result.update(
        ok=ok,
        transport=how,
        error=error,
        subject=subject,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


async def notify(subject: str, body: str, to: str | None = None) -> bool:
    """Send, and never let a mail problem break the request that caused it.

    `to` is for the one email that does not go to Iris: the confirmation a
    reader gets after paying. Everything else leaves it out and lands in the
    club's own inbox.
    """
    how = transport()
    if how is None:
        log.error(
            "email is not configured, so this was NOT sent: %s "
            "(set BREVO_API_KEY, or EMAIL_USER and EMAIL_PASSWORD)",
            subject,
        )
        _record(False, None, subject, "email is not configured")
        return False

    sender = _send_brevo if how == "brevo" else _send_smtp
    try:
        await run_in_threadpool(sender, subject, body, to)
    except Exception as exc:  # noqa: BLE001 — any send failure is non-fatal here
        hint = ""
        if how == "smtp" and isinstance(exc, (OSError, TimeoutError)):
            hint = (
                " — on Render's free tier outbound SMTP is blocked outright; "
                "set BREVO_API_KEY to send over HTTPS instead"
            )
        log.error(
            "email NOT sent via %s to %s: %r: %s%s",
            how, to or "the club inbox", subject, exc, hint,
        )
        _record(False, how, subject, f"{exc}{hint}")
        return False

    log.info("emailed via %s: %s", how, subject)
    _record(True, how, subject)
    return True
