"""Request and response shapes.

Validation lives here so the router stays thin, and so the error messages the
form shows are written in one place.
"""

from __future__ import annotations

import re
from datetime import date, datetime
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
    # How many monthly envelopes they are buying. The price for it comes from
    # the plans table, never from the request — a client cannot name its own.
    plan_months: Literal[1, 3, 6] = 1

    first_name: Str = Field(min_length=1, max_length=60)
    last_name: Str = Field(min_length=1, max_length=60)
    email: EmailStr
    # Split so the number itself is comparable however it is written. The code
    # is what varies between +91, 0091 and 91; the ten digits after it do not.
    phone_cc: Str = Field(default="+91", max_length=6)
    phone_number: Str = Field(min_length=4, max_length=20)
    instagram: Str | None = Field(default=None, max_length=120)

    # Quoted by September's readers. Checked against the phone, never honoured
    # on its own — see the founding lookup in the router.
    promo_code: Str | None = Field(default=None, max_length=32)

    # A subscription bought for somebody else.
    is_gift: bool = False
    gift_message: Str | None = Field(default=None, max_length=600)

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
                     "landmark", "city", "state", "pincode", "country",
                     "promo_code", "gift_message", mode="before")
    @classmethod
    def blank_to_none(cls, v):
        """An untouched optional input arrives as "" — treat it as absent."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("phone_cc")
    @classmethod
    def check_cc(cls, v: str) -> str:
        v = v if v.startswith("+") else "+" + v.lstrip("0")
        if not re.fullmatch(r"\+\d{1,4}", v):
            raise ValueError("A country code looks like +91")
        return v

    @field_validator("phone_number")
    @classmethod
    def check_number(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not 4 <= len(digits) <= 15:
            raise ValueError("That phone number does not look right")
        return digits

    @field_validator("promo_code")
    @classmethod
    def upper_code(cls, v: str | None) -> str | None:
        """Codes are shouted on Instagram; nobody types the case carefully."""
        return v.upper().replace(" ", "") if v else None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def phone(self) -> str:
        return f"{self.phone_cc} {self.phone_number}"

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

        if self.is_gift and not self.gift_message:
            problems["gift_message"] = "Write a line to go in with the gift"

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
    promo_code: str | None = None
    rate_minor: int | None = Field(
        default=None, description="What one month cost them, in paise."
    )
    rate_display: str | None = None
    cycle: str
    plan_months: int
    currency: str | None = None
    amount_minor: int | None = Field(
        default=None, description="In paise (INR) or cents (USD). 27900 = ₹279."
    )
    amount_display: str | None = Field(
        default=None, description="The same amount written for people: '₹279'."
    )
    deliveries_remaining: int = 0


class PaymentOut(BaseModel):
    """What the sign-up form needs to open checkout.

    ``enabled`` is False until Razorpay keys are in .env — the form shows a
    notice instead of a pay button, and nothing is charged.
    """

    enabled: bool
    provider: Literal["razorpay"] = "razorpay"
    amount_minor: int | None = Field(
        default=None,
        description="In paise (INR) or cents (USD) — the unit Razorpay Checkout wants.",
    )
    amount_display: str | None = Field(default=None, description="e.g. '₹279'.")
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
    status: Literal["pending", "active", "expired", "failed", "cancelled"]
    note: Str | None = Field(default=None, max_length=600)


class CycleIn(BaseModel):
    """Overrides one month's sign-up window. Both dates must carry a timezone,
    so there is never a question of whose midnight is meant."""

    opens_at: datetime
    closes_at: datetime
    note: Str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def check_order(self):
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at: the window has to close after it opens")
        return self


class PlanIn(BaseModel):
    """Sets one price.

    **Amounts are in the currency's smallest unit — paise for INR, cents for
    USD.** 27900 is ₹279; 1500 is $15. That is the unit Razorpay's API takes, so
    the value is passed straight through with no conversion anywhere.
    """

    amount_minor: int = Field(
        gt=0,
        le=100_000_000,
        description=(
            "Price in PAISE (INR) or CENTS (USD), not rupees or dollars. "
            "27900 = ₹279. 1500 = $15. Sending 279 would mean ₹2.79."
        ),
        examples=[27900],
    )
    currency: Literal["INR", "USD"] | None = Field(
        default=None,
        description="Defaults to INR for india, USD for international.",
    )
    active: bool = Field(default=True, description="Unset to hide this length from the site.")


class FoundingMemberIn(BaseModel):
    """One of September's readers, who keeps the founding rate for good."""

    first_name: Str = Field(min_length=1, max_length=60)
    last_name: Str | None = Field(default=None, max_length=60)
    phone_cc: Str = Field(default="+91", max_length=6)
    phone_number: Str = Field(min_length=4, max_length=20)
    code: Str = Field(default="FOUNDING15", max_length=32)
    rate_minor: int = Field(default=30000, gt=0, description="Per month, in paise.")
    note: Str | None = Field(default=None, max_length=300)

    @field_validator("code")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper().replace(" ", "")
