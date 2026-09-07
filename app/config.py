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
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    admin_token: str = ""


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
        """The exact origins allowed to call the API.

        Trailing slashes are stripped, because pasting a site's URL from the
        address bar brings one along and a browser never sends it: Chrome puts
        `https://site.com` in the Origin header, so `https://site.com/` in this
        list matches nothing. The site would load, every sign-up would fail with
        a CORS error, and the API log would look perfectly healthy.
        """
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def payments_enabled(self) -> bool:
        """No keys, no checkout. The site is told so and shows a notice rather
        than a broken payment button."""
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 to misread


def make_reference() -> str:
    """Short code the reader can quote back to us: LDP-4F2A9C."""
    return "LDP-" + "".join(random.choices(_ALPHABET, k=6))


def today() -> date:
    return datetime.now().date()
