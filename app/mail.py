"""Sending the one email this club needs: "somebody wants a reminder".

SMTP over the standard library, so there is no extra dependency and no third
party to sign up with. Gmail wants an *app password* rather than the account
password — Google Account > Security > 2-Step Verification > App passwords.

Behind a switch, like Razorpay. With `EMAIL_USER` / `EMAIL_PASSWORD` unset, a
reminder request is still recorded; it simply is not emailed. Filling them in
and restarting starts the sending, with no code change.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from starlette.concurrency import run_in_threadpool

from .config import get_settings

log = logging.getLogger("littledoorpost.mail")


def email_available() -> bool:
    return get_settings().email_enabled


def _send(subject: str, body: str) -> None:
    """Blocking send. Called through a worker thread — SMTP is slow and would
    otherwise hold up the request that triggered it."""
    settings = get_settings()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.email_user
    message["To"] = settings.email_to or settings.email_user
    message.set_content(body)

    with smtplib.SMTP(settings.email_host, settings.email_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.email_user, settings.email_password)
        smtp.send_message(message)


async def notify(subject: str, body: str) -> bool:
    """Send, and never let a mail problem break the request that caused it.

    Somebody asking for a reminder has already been saved by the time this runs.
    If the mailbox is down, or the app password has been revoked, that is worth
    a line in the log — it is not worth turning their request into an error,
    because the record is safe and the list can be read in the admin either way.
    """
    if not email_available():
        log.info("email not configured; not sending: %s", subject)
        return False

    try:
        await run_in_threadpool(_send, subject, body)
        log.info("emailed: %s", subject)
        return True
    except Exception as exc:  # noqa: BLE001 — any SMTP failure is non-fatal here
        log.warning("could not send %r: %s", subject, exc)
        return False
