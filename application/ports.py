"""Application-layer ports (abstract interfaces) for outbound dependencies.

Use cases depend on these; infrastructure implements them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from domain.entities.appointment import Appointment
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty
from domain.value_objects.time_slot import AvailabilityWindow, TimeSlot


# ---- Scraper ----

class OTPRequired(Exception):
    """Raised when an insurer login requires an OTP we don't have yet."""


@dataclass
class OTPChallenge:
    """Opaque handle for an in-progress OTP challenge."""

    session_id: str
    message_to_user: str


class BaseInsurerScraper(ABC):
    """Port: query an insurer's cuadro médico for doctors by specialty + location."""

    @abstractmethod
    async def find_doctors(
        self,
        user: User,
        specialty: Specialty,
        near: Address,
        otp_code: str | None = None,
    ) -> list[Doctor]:
        """Return doctors sorted by distance from `near`.

        Raises OTPRequired if the session needs an SMS code. Caller is expected
        to obtain the code (e.g. via WhatsApp) and call again with otp_code set.
        """


# ---- Voice caller ----

@dataclass
class CallOutcome:
    """Result of a single outbound call to a clinic."""

    doctor: Doctor
    success: bool
    slot: TimeSlot | None = None
    reason: str | None = None  # e.g. "no_answer", "specialty_mismatch", "too_far_out"


class BaseVoiceCaller(ABC):
    @abstractmethod
    async def book_first_available(
        self,
        doctors: list[Doctor],
        on_behalf_of: User,
        constraints: AvailabilityWindow,
    ) -> CallOutcome | None:
        """Dial each doctor in order until a slot is booked. None if all failed."""


# ---- Messaging (WhatsApp) ----

class BaseMessagingClient(ABC):
    @abstractmethod
    async def send_text(self, to_phone: str, body: str) -> None: ...

    @abstractmethod
    async def send_confirmation(self, user: User, appointment: Appointment) -> None: ...

    @abstractmethod
    async def request_otp(self, user: User) -> None:
        """Tell the user to forward the SMS verification code."""


# ---- Calendar ----

class BaseCalendarService(ABC):
    @abstractmethod
    async def get_busy_intervals(
        self, user: User, lookahead_weeks: int
    ) -> list[tuple]:
        """Return list of (start_dt, end_dt) when the user is busy."""


# ---- Geocoding / maps ----

class BaseGeocodingService(ABC):
    @abstractmethod
    async def geocode(self, address: Address) -> Address:
        """Return the address with coordinates filled in."""


# ---- OTP relay (an inbound side-channel: user replies with code) ----

class BaseOTPRelay(ABC):
    @abstractmethod
    async def wait_for_code(self, user_phone: str, timeout_seconds: int) -> str | None:
        """Block until the user sends an OTP via WhatsApp, or timeout."""

    @abstractmethod
    async def submit_code(self, user_phone: str, code: str) -> None:
        """Called by the webhook handler when the user replies with a code."""
