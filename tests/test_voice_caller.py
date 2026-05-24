"""Tests for the ElevenLabs voice caller (mocked — no real calls in CI)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from application.ports import CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import InsurerCredentials, User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty, SpecialtyType
from domain.value_objects.time_slot import AvailabilityWindow, TimeSlot
from infrastructure.voice import call_session as call_session_mod
from infrastructure.voice.elevenlabs_voice_caller import (
    ElevenLabsVoiceCaller,
    VoiceCallerConfig,
)


def _make_doctor(phone: str = "+34600000001", name: str = "Dr. Test") -> Doctor:
    return Doctor(
        clinic_id="c1",
        practitioner_id="p1",
        name=name,
        specialty=Specialty(name="PSICOLOGIA", type=SpecialtyType.SPECIALTY),
        clinic_name="Clínica Test",
        address=Address(raw="Calle Test 1, Madrid 28001", city="Madrid"),
        phone=phone,
    )


def _make_user() -> User:
    return User(
        phone="+34612345678",
        name="Test User",
        home_address=Address(raw="Home", city="Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer="cigna", username="X1234567A", password="test"
        ),
    )


def _make_config(**overrides) -> VoiceCallerConfig:
    defaults = dict(
        elevenlabs_api_key="el_test_key",
        elevenlabs_agent_id="seng_test123",
        elevenlabs_phone_number_id="phnum_test123",
        anthropic_api_key="sk-ant-test",
    )
    defaults.update(overrides)
    return VoiceCallerConfig(**defaults)


class TestElevenLabsVoiceCaller:
    """Unit tests with mocked external services."""

    @pytest.mark.asyncio
    async def test_book_first_available_returns_none_when_all_fail(self):
        """When run_call_session returns failure for all doctors, returns None."""
        caller = ElevenLabsVoiceCaller(config=_make_config())
        doctors = [_make_doctor("+34600000001"), _make_doctor("+34600000002")]
        user = _make_user()
        constraints = AvailabilityWindow()

        failure = CallOutcome(
            doctor=doctors[0], success=False, reason="no_answer"
        )

        with patch.object(
            call_session_mod, "run_call_session",
            new_callable=AsyncMock,
            return_value=failure,
        ):
            result = await caller.book_first_available(
                doctors, user, constraints
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_book_first_available_returns_first_success(self):
        """Stops at the first successful booking."""
        caller = ElevenLabsVoiceCaller(config=_make_config())
        d1 = _make_doctor("+34600000001", "Dr. Fail")
        d2 = _make_doctor("+34600000002", "Dr. Success")
        user = _make_user()
        constraints = AvailabilityWindow()

        from datetime import datetime

        success_outcome = CallOutcome(
            doctor=d2,
            success=True,
            slot=TimeSlot(start=datetime(2026, 5, 27, 10, 0)),
        )
        fail_outcome = CallOutcome(
            doctor=d1, success=False, reason="no_answer"
        )

        call_count = 0

        async def mock_call_session(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_outcome
            return success_outcome

        with patch.object(
            call_session_mod, "run_call_session",
            side_effect=mock_call_session,
        ):
            result = await caller.book_first_available(
                [d1, d2], user, constraints
            )

        assert result is not None
        assert result.success is True
        assert result.slot is not None
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_demo_mode_routes_to_demo_number(self):
        """In DEMO_MODE, calls go to the demo receptionist number."""
        config = _make_config(
            demo_mode=True, demo_receptionist_number="+15559999999"
        )
        caller = ElevenLabsVoiceCaller(config=config)
        doctor = _make_doctor("+34600000001")

        resolved = caller._resolve_phone(doctor)
        assert resolved == "+15559999999"

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self):
        """If a call times out, returns a failure outcome."""
        caller = ElevenLabsVoiceCaller(config=_make_config())
        doctor = _make_doctor()
        user = _make_user()
        constraints = AvailabilityWindow()

        async def slow_call(**kwargs):
            await asyncio.sleep(999)

        with patch.object(
            call_session_mod, "run_call_session",
            side_effect=slow_call,
        ), patch(
            "infrastructure.voice.elevenlabs_voice_caller._CONVERSATION_TIMEOUT",
            0.01,
        ):
            result = await caller.book_first_available(
                [doctor], user, constraints
            )

        assert result is None  # all failed → None

    @pytest.mark.asyncio
    async def test_no_phone_returns_failure(self):
        """Doctor with empty phone returns no_phone_number reason."""
        caller = ElevenLabsVoiceCaller(config=_make_config())
        doctor = _make_doctor(phone="")
        user = _make_user()
        constraints = AvailabilityWindow()

        result = await caller.book_first_available([doctor], user, constraints)
        assert result is None


class TestSpeechEngineHandler:
    """Tests for the LLM prompt building and outcome parsing."""

    def test_build_booking_prompt_includes_user_and_specialty(self):
        from infrastructure.voice.speech_engine_handler import (
            build_booking_system_prompt,
        )

        user = _make_user()
        doctor = _make_doctor()
        constraints = AvailabilityWindow(max_weeks_out=2)

        prompt = build_booking_system_prompt(user, doctor, constraints)
        assert "Test User" in prompt
        assert "PSICOLOGIA" in prompt
        assert "2 semanas" in prompt

    def test_parse_booking_outcome_success(self):
        from infrastructure.voice.speech_engine_handler import (
            parse_booking_outcome,
        )

        doctor = _make_doctor()
        success, slot, reason = parse_booking_outcome(
            "Perfecto. CITA CONFIRMADA: martes 27 de mayo a las 10:00.",
            doctor,
        )
        assert success is True
        assert slot is not None
        assert reason is None

    def test_parse_booking_outcome_no_availability(self):
        from infrastructure.voice.speech_engine_handler import (
            parse_booking_outcome,
        )

        doctor = _make_doctor()
        success, slot, reason = parse_booking_outcome(
            "Lo siento, SIN DISPONIBILIDAD esta semana. Gracias.",
            doctor,
        )
        assert success is False
        assert slot is None
        assert reason == "no_availability"

    def test_parse_booking_outcome_unclear(self):
        from infrastructure.voice.speech_engine_handler import (
            parse_booking_outcome,
        )

        doctor = _make_doctor()
        success, slot, reason = parse_booking_outcome(
            "Hmm, déjeme consultar con el doctor...",
            doctor,
        )
        assert success is False
        assert reason == "conversation_unclear"


class TestVoiceWebhook:
    """Tests for the voice webhook endpoints."""

    @pytest.mark.asyncio
    async def test_status_callback_returns_204(self):
        from fastapi.testclient import TestClient

        from main import create_app

        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/voice/status",
            data={"CallSid": "CA_test_123", "CallStatus": "no-answer"},
        )
        assert response.status_code == 204
