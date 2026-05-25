"""Use case: book a private health appointment for a user."""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from application.ports import (
    BaseCalendarService,
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
from domain.value_objects.time_slot import AvailabilityWindow

logger = logging.getLogger(__name__)


@dataclass
class AppointmentRequest:
    specialty: Specialty
    raw_query: str  # original user message, for context
    max_weeks: int | None = None  # per-request urgency override; None → use user.availability.max_weeks_out
    gender_preference: str | None = None  # "female", "male", or None (no preference)


class BookAppointmentUseCase:
    def __init__(
        self,
        scraper: BaseInsurerScraper,
        voice_caller: BaseVoiceCaller,
        appointment_repo: AppointmentRepository,
        whatsapp: BaseMessagingClient,
        otp_relay: BaseOTPRelay,
        calendar: BaseCalendarService | None = None,
        calendar_auth_url: str = "",
        otp_timeout_seconds: int = 300,
    ):
        self._scraper = scraper
        self._voice_caller = voice_caller
        self._appointment_repo = appointment_repo
        self._whatsapp = whatsapp
        self._otp_relay = otp_relay
        self._calendar = calendar
        self._calendar_auth_url = calendar_auth_url
        self._otp_timeout_seconds = otp_timeout_seconds

    async def execute(self, user: User, request: AppointmentRequest) -> Appointment | None:
        # Block booking until Google Calendar is connected
        if (
            self._calendar
            and self._calendar_auth_url
            and not user.google_calendar_token
        ):
            from urllib.parse import quote

            link = f"{self._calendar_auth_url}?phone={quote(user.phone, safe='')}"
            await self._whatsapp.send_text(
                user.phone,
                "Before I can book, I need access to your Google Calendar "
                "so I can check your availability and avoid conflicts.\n\n"
                f"Please connect it here:\n{link}\n\n"
                "Once connected, send your request again and I'll get started.",
            )
            return None

        await self._whatsapp.send_text(
            user.phone,
            f"Looking for {request.specialty.name.lower()} appointments near you...",
        )

        # Fetch calendar busy intervals (if calendar is connected)
        busy_intervals = await self._fetch_busy_intervals(user, request)

        doctors = await self._find_doctors_handling_otp(user, request)
        if not doctors:
            await self._whatsapp.send_text(
                user.phone,
                "I couldn't find any clinics for that specialty near you. Try another?",
            )
            return None

        # Filter doctors by gender preference if specified
        if request.gender_preference:
            filtered = _filter_doctors_by_gender(doctors, request.gender_preference)
            if filtered:
                logger.info(
                    "Gender filter (%s): %d → %d doctors",
                    request.gender_preference,
                    len(doctors),
                    len(filtered),
                )
                doctors = filtered
            else:
                logger.warning(
                    "Gender filter (%s) would remove all doctors — keeping full list",
                    request.gender_preference,
                )

        logger.info("Found %d doctors, starting call loop", len(doctors))
        constraints = _constraints_for_request(user, request)

        # Store per-call state for the voice prompt
        from infrastructure.voice.call_session import (
            set_busy_intervals,
            set_doctor_gender,
            set_insurance_id,
            set_insurer_name,
            set_max_weeks_out,
            set_patient_name,
            set_patient_phone,
        )

        set_patient_name(user.name)
        set_max_weeks_out(constraints.max_weeks_out)
        set_doctor_gender(request.gender_preference or "")
        set_insurer_name(user.insurer.value.capitalize())
        set_insurance_id(user.insurer_credentials.username)
        set_patient_phone(user.phone)
        if busy_intervals:
            logger.info(
                "User has %d busy intervals — voice caller will avoid conflicts",
                len(busy_intervals),
            )
            formatted = [
                _format_busy_interval(s, e)
                for s, e in busy_intervals
            ]
            set_busy_intervals(formatted)
        else:
            set_busy_intervals([])
        outcome = await self._voice_caller.book_first_available(
            doctors,
            on_behalf_of=user,
            constraints=constraints,
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

        # Add event to Google Calendar (if connected)
        await self._add_calendar_event(user, appointment)

        await self._whatsapp.send_confirmation(user, appointment)
        return appointment

    async def _fetch_busy_intervals(self, user: User, request: AppointmentRequest):
        """Fetch busy intervals from user's calendar, if available."""
        if not self._calendar or not user.google_calendar_token:
            return []
        weeks = request.max_weeks or user.availability.max_weeks_out
        try:
            return await self._calendar.get_busy_intervals(user, weeks)
        except Exception:
            logger.exception("Calendar lookup failed for %s", user.phone)
            return []

    async def _add_calendar_event(self, user: User, appointment: Appointment):
        """Create a calendar event for the confirmed appointment."""
        if not self._calendar:
            return
        try:
            event_id = await self._calendar.add_event(user, appointment)
            if event_id:
                await self._whatsapp.send_text(
                    user.phone,
                    "Added to your Google Calendar.",
                )
        except Exception:
            logger.exception("Failed to add calendar event for %s", user.phone)

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


def _constraints_for_request(
    user: User, request: AppointmentRequest
) -> AvailabilityWindow:
    """Build the AvailabilityWindow the voice caller should respect for
    this booking. If the request's intent parser inferred an urgency
    (`max_weeks` set), override the user's default max_weeks_out for this
    one booking only — preserving all the other prefs (preferred-time,
    excluded days, travel buffer, etc.) from the user's profile.
    """
    if request.max_weeks is None:
        return user.availability
    return dataclasses.replace(
        user.availability, max_weeks_out=request.max_weeks
    )


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _unknown_slot():
    from datetime import datetime

    from domain.value_objects.time_slot import TimeSlot
    return TimeSlot(start=datetime.utcnow(), duration_minutes=0)


def _format_busy_interval(s, e) -> str:
    """Format a busy interval for the voice prompt.

    All-day events (midnight to midnight) are shown as 'Mon 25 May TODO EL DÍA'.
    Regular events show the time range.
    """
    if s.hour == 0 and s.minute == 0 and e.hour == 0 and e.minute == 0:
        return f"{s.strftime('%a %d %b')} TODO EL DÍA (ocupado)"
    return f"{s.strftime('%a %d %b %H:%M')}–{e.strftime('%H:%M')}"


# Common Spanish female first names (uppercase, no accents needed — names
# from Cigna's API are already uppercased).
_FEMALE_NAMES: frozenset[str] = frozenset({
    "MARÍA", "MARIA", "CARMEN", "ANA", "ROSA", "ISABEL", "SOLEDAD",
    "LAURA", "TERESA", "ELENA", "MARTA", "CRISTINA", "PATRICIA",
    "PILAR", "BEATRIZ", "LUCÍA", "LUCIA", "ALICIA", "RAQUEL",
    "SUSANA", "SILVIA", "NURIA", "IRENE", "SARA", "ALBA",
    "PAULA", "SONIA", "NATALIA", "ROCÍO", "ROCIO", "ANDREA",
    "MARINA", "INÉS", "INES", "CONSUELO", "DOLORES", "INMACULADA",
    "PALOMA", "VERÓNICA", "VERONICA", "VANESSA", "MÓNICA", "MONICA",
    "ESTHER", "EVA", "OLGA", "LOURDES", "AMPARO", "ILUMINADA",
    "YOLANDA", "JULIA", "VICTORIA", "CLAUDIA", "CAROLINA",
    "ALMUDENA", "BLANCA", "DIANA", "VIRGINIA", "MARGARITA",
    "CONCEPCIÓN", "CONCEPCION", "MERCEDES", "ANTONIA", "EMILIA",
    "CLARA", "SOFÍA", "SOFIA", "EMMA", "MIRIAM", "NOELIA",
    "LORENA", "SANDRA", "FÁTIMA", "FATIMA", "ADRIANA",
    "ESMERALDA", "BEGOÑA", "BEGONA", "ALEJANDRA", "NEREA",
    "LARA", "REBECA", "LETICIA", "REMEDIOS", "JOSEFA", "JUANA",
    "ÁNGELA", "ANGELA", "CELIA", "ROSARIO", "ASUNCIÓN", "ASUNCION",
})

_MALE_NAMES: frozenset[str] = frozenset({
    "CARLOS", "PEDRO", "JUAN", "LUIS", "JOSÉ", "JOSE", "FRANCISCO",
    "MIGUEL", "ANTONIO", "JAVIER", "DAVID", "MANUEL", "RAFAEL",
    "FERNANDO", "JORGE", "PABLO", "ALBERTO", "ALEJANDRO", "SERGIO",
    "ANDRÉS", "ANDRES", "RAMÓN", "RAMON", "DIEGO", "ENRIQUE",
    "RICARDO", "EMILIO", "ÁNGEL", "ANGEL", "SANTIAGO", "VÍCTOR",
    "VICTOR", "EDUARDO", "ROBERTO", "IGNACIO", "ÁLVARO", "ALVARO",
    "HÉCTOR", "HECTOR", "TOMÁS", "TOMAS", "IVÁN", "IVAN",
    "GONZALO", "RUBÉN", "RUBEN", "RAÚL", "RAUL", "ADRIÁN", "ADRIAN",
    "GUILLERMO", "JESÚS", "JESUS", "DANIEL", "MARCOS", "MARTÍN",
    "MARTIN", "JAIME", "ALFREDO", "FÉLIX", "FELIX", "AGUSTÍN",
    "AGUSTIN", "BERNARDO", "GABRIEL",
})


def _infer_doctor_gender(name: str) -> str | None:
    """Infer gender from a Cigna doctor name like 'HERMOSO IZQUIERDO, SOLEDAD'.

    Returns 'female', 'male', or None if we can't tell.
    """
    parts = name.split(",")
    if len(parts) < 2:
        return None
    first_name_part = parts[1].strip().split()[0] if parts[1].strip() else ""
    if not first_name_part:
        return None
    if first_name_part in _FEMALE_NAMES:
        return "female"
    if first_name_part in _MALE_NAMES:
        return "male"
    return None


def _filter_doctors_by_gender(
    doctors: list,
    preference: str,
) -> list:
    """Keep only doctors whose inferred gender matches the preference."""
    return [
        d for d in doctors
        if _infer_doctor_gender(d.name) == preference
    ]
