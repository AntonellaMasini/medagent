"""End-to-end test: English text → intent parsing → specialty → voice call.

Exercises the full booking pipeline with mocked infrastructure so you can
run `uv run pytest tests/test_e2e_booking_flow.py -v` to validate each PR
integrates correctly without needing real Twilio/ElevenLabs/Cigna credentials.

Flow:
  1. User sends "book a psychologist" via WhatsApp
  2. Intent parser extracts PSICOLOGIA specialty
  3. Scraper returns a list of doctors (mocked)
  4. Voice caller dials doctors and books a slot (mocked)
  5. WhatsApp confirmation is sent back to the user
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from application.book_appointment import BookAppointmentUseCase
from application.intent_parser import parse_appointment_intent
from application.ports import CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty, SpecialtyType
from domain.value_objects.time_slot import TimeSlot


def _make_user() -> User:
    return User(
        phone="+34612345678",
        name="Sarah Johnson",
        home_address=Address(raw="Calle Serrano 50, Madrid", city="Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer.CIGNA,
            username="X1234567A",
            password="test_pass",
        ),
    )


def _make_doctors() -> list[Doctor]:
    return [
        Doctor(
            clinic_id="c1",
            practitioner_id="p1",
            name="Dra. María López",
            specialty=Specialty(name="PSICOLOGIA", type=SpecialtyType.SPECIALTY),
            clinic_name="Centro Psicológico Madrid",
            address=Address(raw="Calle Gran Vía 30, Madrid", city="Madrid"),
            phone="+34911234567",
        ),
        Doctor(
            clinic_id="c2",
            practitioner_id="p2",
            name="Dr. Carlos Ruiz",
            specialty=Specialty(name="PSICOLOGIA", type=SpecialtyType.SPECIALTY),
            clinic_name="Clínica Bienestar",
            address=Address(raw="Calle Alcalá 100, Madrid", city="Madrid"),
            phone="+34912345678",
        ),
    ]


class TestE2EBookingFlow:
    """Full pipeline test: text input → appointment confirmation."""

    @pytest.mark.asyncio
    async def test_english_psychologist_request_books_appointment(self):
        """'book a psychologist' should parse, find doctors, call, and book."""
        user = _make_user()
        doctors = _make_doctors()

        # Step 1: Intent parsing (real, no mocks)
        request = parse_appointment_intent("book a psychologist")
        assert request is not None
        assert request.specialty.name == "PSICOLOGIA"

        # Step 2-5: Use case with mocked infrastructure
        mock_scraper = AsyncMock()
        mock_scraper.find_doctors.return_value = doctors

        booked_slot = TimeSlot(start=datetime(2026, 5, 27, 10, 0))
        mock_voice_caller = AsyncMock()
        mock_voice_caller.book_first_available.return_value = CallOutcome(
            doctor=doctors[0],
            success=True,
            slot=booked_slot,
        )

        mock_whatsapp = AsyncMock()
        mock_appointment_repo = AsyncMock()
        mock_otp_relay = AsyncMock()

        use_case = BookAppointmentUseCase(
            scraper=mock_scraper,
            voice_caller=mock_voice_caller,
            appointment_repo=mock_appointment_repo,
            whatsapp=mock_whatsapp,
            otp_relay=mock_otp_relay,
        )

        appointment = await use_case.execute(user, request)

        # Verify the full chain worked
        assert appointment is not None
        assert appointment.slot == booked_slot
        assert appointment.doctor.name == "Dra. María López"
        assert appointment.doctor.specialty.name == "PSICOLOGIA"

        # Voice caller was invoked with the doctors list
        mock_voice_caller.book_first_available.assert_called_once()
        call_args = mock_voice_caller.book_first_available.call_args
        assert len(call_args.args[0]) == 2  # both doctors passed
        assert call_args.kwargs["on_behalf_of"] == user

        # WhatsApp confirmation was sent
        mock_whatsapp.send_confirmation.assert_called_once()

        # Appointment was persisted
        mock_appointment_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_english_cardiologist_request_sets_urgent_max_weeks(self):
        """'cardiologist appointment' should resolve + set 1-week urgency."""
        request = parse_appointment_intent("cardiologist appointment please")
        assert request is not None
        assert request.specialty.name == "CARDIOLOGÍA"
        # Cardiología is high-risk → 1-week urgency
        assert request.max_weeks == 1

    @pytest.mark.asyncio
    async def test_no_doctors_found_sends_whatsapp_message(self):
        """When scraper returns empty, user gets a WhatsApp 'not found' msg."""
        user = _make_user()
        request = parse_appointment_intent("book a dermatologist")
        assert request is not None

        mock_scraper = AsyncMock()
        mock_scraper.find_doctors.return_value = []

        mock_voice_caller = AsyncMock()
        mock_whatsapp = AsyncMock()
        mock_appointment_repo = AsyncMock()
        mock_otp_relay = AsyncMock()

        use_case = BookAppointmentUseCase(
            scraper=mock_scraper,
            voice_caller=mock_voice_caller,
            appointment_repo=mock_appointment_repo,
            whatsapp=mock_whatsapp,
            otp_relay=mock_otp_relay,
        )

        result = await use_case.execute(user, request)

        # No appointment created
        assert result is None

        # Voice caller was NOT invoked (no doctors to call)
        mock_voice_caller.book_first_available.assert_not_called()

        # User was notified via WhatsApp
        assert mock_whatsapp.send_text.call_count == 2  # "Looking..." + "couldn't find"

    @pytest.mark.asyncio
    async def test_all_calls_fail_sends_failure_whatsapp(self):
        """When voice caller fails all doctors, user gets a failure message."""
        user = _make_user()
        doctors = _make_doctors()
        request = parse_appointment_intent("book a psychologist")
        assert request is not None

        mock_scraper = AsyncMock()
        mock_scraper.find_doctors.return_value = doctors

        mock_voice_caller = AsyncMock()
        mock_voice_caller.book_first_available.return_value = None

        mock_whatsapp = AsyncMock()
        mock_appointment_repo = AsyncMock()
        mock_otp_relay = AsyncMock()

        use_case = BookAppointmentUseCase(
            scraper=mock_scraper,
            voice_caller=mock_voice_caller,
            appointment_repo=mock_appointment_repo,
            whatsapp=mock_whatsapp,
            otp_relay=mock_otp_relay,
        )

        result = await use_case.execute(user, request)

        # Appointment created but with FAILED status
        assert result is not None
        assert result.status.value == "failed"

        # Voice caller WAS invoked
        mock_voice_caller.book_first_available.assert_called_once()

        # User notified about failure
        failure_msg = mock_whatsapp.send_text.call_args_list[-1].args[1]
        assert "couldn't get a slot" in failure_msg

    @pytest.mark.asyncio
    async def test_spanish_input_also_works_e2e(self):
        """'necesito un psicologo' also routes through the full flow."""
        request = parse_appointment_intent("necesito un psicologo")
        assert request is not None
        assert request.specialty.name == "PSICOLOGIA"

    @pytest.mark.asyncio
    async def test_dentist_english_routes_to_odontologia(self):
        """'dentist' (English) maps to ODONTOLOGIA through full flow."""
        request = parse_appointment_intent("I need a dentist")
        assert request is not None
        assert request.specialty.name == "ODONTOLOGIA"
