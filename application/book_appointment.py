"""Use case: book a private health appointment for a user."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from application.ports import (
    BaseInsurerScraper,
    BaseMessagingClient,
    BaseOTPRelay,
    BaseVoiceCaller,
    OTPRequired,
)
from domain.entities.appointment import Appointment, AppointmentStatus
from domain.entities.user import User
from domain.repositories.appointment_repository import AppointmentRepository
from domain.value_objects.specialty import Specialty

logger = logging.getLogger(__name__)


@dataclass
class AppointmentRequest:
    specialty: Specialty
    raw_query: str  # original user message, for context


class BookAppointmentUseCase:
    def __init__(
        self,
        scraper: BaseInsurerScraper,
        voice_caller: BaseVoiceCaller,
        appointment_repo: AppointmentRepository,
        whatsapp: BaseMessagingClient,
        otp_relay: BaseOTPRelay,
        otp_timeout_seconds: int = 300,
    ):
        self._scraper = scraper
        self._voice_caller = voice_caller
        self._appointment_repo = appointment_repo
        self._whatsapp = whatsapp
        self._otp_relay = otp_relay
        self._otp_timeout_seconds = otp_timeout_seconds

    async def execute(self, user: User, request: AppointmentRequest) -> Appointment | None:
        await self._whatsapp.send_text(
            user.phone,
            f"Looking for {request.specialty.name.lower()} appointments near you...",
        )

        doctors = await self._find_doctors_handling_otp(user, request)
        if not doctors:
            await self._whatsapp.send_text(
                user.phone,
                "I couldn't find any clinics for that specialty near you. Try another?",
            )
            return None

        logger.info("Found %d doctors, starting call loop", len(doctors))
        outcome = await self._voice_caller.book_first_available(
            doctors, on_behalf_of=user, constraints=user.availability
        )

        if outcome is None or not outcome.success or outcome.slot is None:
            await self._whatsapp.send_text(
                user.phone,
                f"I tried {len(doctors)} clinics but couldn't get a slot. Want me to "
                "widen the search (further away, or different time windows)?",
            )
            appt = Appointment(
                id=_new_id(),
                user_phone=user.phone,
                doctor=doctors[0],
                slot=_unknown_slot(),
                status=AppointmentStatus.FAILED,
                notes=f"Exhausted {len(doctors)} clinics",
            )
            await self._appointment_repo.save(appt)
            return appt

        appointment = Appointment.create(user, outcome.doctor, outcome.slot)
        await self._appointment_repo.save(appointment)
        await self._whatsapp.send_confirmation(user, appointment)
        return appointment

    async def _find_doctors_handling_otp(self, user: User, request: AppointmentRequest):
        """Run scraper, providing an inline OTP callback so the browser
        stays open across the OTP wait. Okta-style OTP flows (Cigna)
        invalidate the code if the session restarts.
        """
        async def wait_for_otp() -> str | None:
            await self._whatsapp.request_otp(user)
            code = await self._otp_relay.wait_for_code(
                user.phone, self._otp_timeout_seconds
            )
            if code is None:
                await self._whatsapp.send_text(
                    user.phone,
                    "I didn't get the code in time. Send 'cita' to start over.",
                )
            return code

        try:
            return await self._scraper.find_doctors(
                user=user,
                specialty=request.specialty,
                near=user.home_address,
                wait_for_otp=wait_for_otp,
            )
        except OTPRequired:
            # The callback didn't return a code (timeout, etc.). The user
            # has already been notified inside wait_for_otp.
            return []


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _unknown_slot():
    from datetime import datetime

    from domain.value_objects.time_slot import TimeSlot
    return TimeSlot(start=datetime.utcnow(), duration_minutes=0)
