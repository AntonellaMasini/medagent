"""Outbound voice caller using ElevenLabs Speech Engine + Twilio.

Architecture:
  1. register_call() tells ElevenLabs about the upcoming call → TwiML.
  2. Twilio REST API dials the clinic with that TwiML.
  3. Twilio streams audio to ElevenLabs via <Connect><Stream>.
  4. ElevenLabs Speech Engine handles STT/TTS/turn-taking.
  5. ElevenLabs connects to our /v1/chat/completions endpoint where
     Claude (Anthropic) conducts the booking conversation in Spanish.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from application.ports import BaseVoiceCaller, CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import AvailabilityWindow
from infrastructure.voice import call_session

logger = logging.getLogger(__name__)

# How long to wait for a call to connect before giving up (seconds).
_CALL_CONNECT_TIMEOUT = 30
# How long a conversation can last before we force-end it (seconds).
# Phone conversations with receptionists can easily take 3-5 minutes.
_CONVERSATION_TIMEOUT = 300


@dataclass
class VoiceCallerConfig:
    """All config needed by the voice caller, passed from main.py."""

    elevenlabs_api_key: str
    elevenlabs_agent_id: str
    elevenlabs_phone_number_id: str
    anthropic_api_key: str
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_voice_number: str = ""
    base_url: str = ""
    demo_mode: bool = False
    demo_receptionist_number: str = ""


class ElevenLabsVoiceCaller(BaseVoiceCaller):
    """Real voice caller: dials clinics via Twilio, speaks via Speech Engine.

    Iterates doctors in order, dials each, waits for outcome. Returns the
    first successful booking or None if all failed.
    """

    def __init__(self, config: VoiceCallerConfig) -> None:
        self._config = config

    async def book_first_available(
        self,
        doctors: list[Doctor],
        on_behalf_of: User,
        constraints: AvailabilityWindow,
    ) -> CallOutcome | None:
        for doctor in doctors:
            logger.info(
                "Dialing %s at %s (%s)",
                doctor.name,
                doctor.clinic_name,
                doctor.phone,
            )
            outcome = await self._attempt_call(doctor, on_behalf_of, constraints)
            if outcome.success:
                return outcome
            logger.info(
                "Call to %s failed: %s. Trying next.",
                doctor.name,
                outcome.reason,
            )
        return None

    async def _attempt_call(
        self,
        doctor: Doctor,
        user: User,
        constraints: AvailabilityWindow,
    ) -> CallOutcome:
        """Place a single outbound call and run the booking conversation."""
        phone = self._resolve_phone(doctor)
        if not phone:
            return CallOutcome(
                doctor=doctor, success=False, reason="no_phone_number"
            )

        try:
            result = await asyncio.wait_for(
                call_session.run_call_session(
                    config=self._config,
                    doctor=doctor,
                    user=user,
                    constraints=constraints,
                    to_phone=phone,
                ),
                timeout=_CONVERSATION_TIMEOUT,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("Call to %s timed out", doctor.name)
            return CallOutcome(doctor=doctor, success=False, reason="timeout")
        except Exception as exc:
            logger.exception("Call to %s failed with error", doctor.name)
            return CallOutcome(
                doctor=doctor, success=False, reason=f"error: {exc}"
            )

    def _resolve_phone(self, doctor: Doctor) -> str:
        """In DEMO_MODE, route to the demo receptionist number instead."""
        if self._config.demo_mode and self._config.demo_receptionist_number:
            return self._config.demo_receptionist_number
        return doctor.phone
