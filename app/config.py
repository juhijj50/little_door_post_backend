"""Settings, read once from backend/.env."""

from __future__ import annotations

import random
import string
from datetime import date, datetime
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = ""
    port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    admin_token: str = ""

    subscription_price_inr: int = 499
    signup_window: str = "Automatic"

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    @property
    def dsn(self) -> str:
        """The connection string, in the one dialect psycopg understands.

        Neon's console hands out several flavours of the same URL. The
        SQLAlchemy one — ``postgresql+psycopg://…`` — is the default on some
        tabs, and psycopg rejects it outright ("invalid connection option").
        Strip the ``+driver`` suffix instead of making anyone spot it.
        """
        url = self.database_url.strip()
        if not url:
            return ""
        scheme, sep, rest = url.partition("://")
        if sep and "+" in scheme:
            url = f"{scheme.split('+', 1)[0]}{sep}{rest}"
        return url

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def payments_enabled(self) -> bool:
        """No keys, no checkout. The site is told so and shows a notice rather
        than a broken payment button."""
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def signup_open(now: datetime | None = None) -> bool:
    """Sign-ups run from the 20th of one month to the 2nd of the next."""
    mode = get_settings().signup_window
    if mode == "Open now":
        return True
    if mode == "Closed":
        return False
    day = (now or datetime.now()).day
    return day >= 20 or day <= 2


def current_cycle(now: datetime | None = None) -> str:
    """The month a sign-up belongs to, as 'YYYY-MM'.

    Sign-ups on the 1st or 2nd are closing out the window that opened on the
    20th of the previous month, so they count against that month.
    """
    now = now or datetime.now()
    year, month = now.year, now.month
    if now.day <= 2:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return f"{year}-{month:02d}"


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 to misread


def make_reference() -> str:
    """Short code the reader can quote back to us: LDP-4F2A9C."""
    return "LDP-" + "".join(random.choices(_ALPHABET, k=6))


def today() -> date:
    return datetime.now().date()
