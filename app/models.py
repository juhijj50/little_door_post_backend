"""Request and response shapes.

Validation lives here so the router stays thin, and so the error messages the
form shows are written in one place.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

Str = Annotated[str, Field(strip_whitespace=True)]

PHONE_RE = re.compile(r"^\+?[\d\s().-]{6,24}$")
PINCODE_RE = re.compile(r"^[1-9]\d{5}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

MAX_INTERESTS = 12

# Joins several field errors into the single message a model validator can
# raise; main.py splits it back out into {field: message}.
FIELD_SEPARATOR = " ;; "


class SubscriberIn(BaseModel):
    """One reader's sign-up. Address fields are required for India and optional
    for the international waitlist — see the model validator at the bottom."""

    region: Literal["india", "international"]

    full_name: Str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: Str = Field(min_length=1, max_length=24)
    instagram: Str | None = Field(default=None, max_length=120)

    birthdate: date | None = None
    interests: list[Str] = Field(default_factory=list, max_length=MAX_INTERESTS)
    interests_note: Str | None = Field(default=None, max_length=600)

    address_line1: Str | None = Field(default=None, max_length=200)
    address_line2: Str | None = Field(default=None, max_length=200)
    landmark: Str | None = Field(default=None, max_length=160)
    city: Str | None = Field(default=None, max_length=120)
    state: Str | None = Field(default=None, max_length=120)
    pincode: Str | None = Field(default=None, max_length=24)
    country: Str | None = Field(default=None, max_length=120)

    @field_validator("instagram", "interests_note", "address_line1", "address_line2",
                     "landmark", "city", "state", "pincode", "country", mode="before")
    @classmethod
    def blank_to_none(cls, v):
        """An untouched optional input arrives as "" — treat it as absent."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v: str) -> str:
        if not PHONE_RE.match(v):
            raise ValueError("That phone number does not look right")
        return v

    @field_validator("instagram")
    @classmethod
    def clean_handle(cls, v: str | None) -> str | None:
        """Accept "@name", "name", or a full profile URL — store the handle."""
        if v is None:
            return None
        handle = re.sub(r"^https?://(www\.)?instagram\.com/", "", v, flags=re.I)
        handle = re.split(r"[/?]", handle)[0].lstrip("@").strip()
        if not handle:
            return None
        if not HANDLE_RE.match(handle):
            raise ValueError("That Instagram handle has characters Instagram does not allow")
        return handle

    @field_validator("birthdate")
    @classmethod
    def check_birthdate(cls, v: date | None) -> date | None:
        """A typo'd year is worse than no year at all."""
        if v is None:
            return None
        if v >= date.today() or v.year < 1900:
            raise ValueError("That birthdate does not look right")
        return v

    @field_validator("interests")
    @classmethod
    def clean_interests(cls, v: list[str]) -> list[str]:
        return [i.strip() for i in v if i and i.strip()][:MAX_INTERESTS]

    @model_validator(mode="after")
    def check_address(self):
        """Region-specific rules, reported together.

        Every problem goes into one message joined by ``FIELD_SEPARATOR`` so the
        reader sees the whole list at once — raising on the first one would send
        them round the form a field at a time.
        """
        problems: dict[str, str] = {}

        if self.region == "india":
            required = {
                "address_line1": "Street address is required",
                "city": "City is required",
                "state": "State is required",
                # Sign-ups come through the Instagram bio, and it is how Iris
                # recognises a reader who writes back.
                "instagram": "Instagram handle is required",
            }
            for field, message in required.items():
                if not getattr(self, field):
                    problems[field] = message
            if not self.pincode or not PINCODE_RE.match(self.pincode):
                problems["pincode"] = "An Indian PIN code is six digits"
            self.country = "India"
        elif not self.country:
            problems["country"] = "Country is required"

        if problems:
            raise ValueError(
                FIELD_SEPARATOR.join(f"{k}: {v}" for k, v in problems.items())
            )
        return self


class SubscriptionOut(BaseModel):
    id: str
    reference: str
    region: str
    status: str
    full_name: str
    amount_inr: int | None
    cycle: str


class PaymentOut(BaseModel):
    """What the sign-up form needs to open checkout.

    ``enabled`` is False until Razorpay keys are in .env — the form shows a
    notice instead of a pay button, and nothing is charged.
    """

    enabled: bool
    provider: Literal["razorpay"] = "razorpay"
    amount_inr: int | None = None
    currency: str = "INR"
    key_id: str | None = None
    order_id: str | None = None
    message: str | None = None


class SubscribeResponse(BaseModel):
    subscription: SubscriptionOut
    payment: PaymentOut | None = None


class RazorpayVerifyIn(BaseModel):
    """The three values Razorpay Checkout hands back on success."""

    razorpay_order_id: Str = Field(min_length=1, max_length=80)
    razorpay_payment_id: Str = Field(min_length=1, max_length=80)
    razorpay_signature: Str = Field(min_length=1, max_length=200)


class AdminStatusIn(BaseModel):
    status: Literal["pending", "paid", "failed", "cancelled", "waitlist"]
    note: Str | None = Field(default=None, max_length=600)
