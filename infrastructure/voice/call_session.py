"""Single-call session: places call via ElevenLabs outbound_call API.

ElevenLabs natively handles the Twilio audio bridge — we just tell it
which Speech Engine agent to use and which number to call.  The Speech
Engine then connects back to our /ws handler for LLM logic.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from application.ports import CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import AvailabilityWindow

logger = logging.getLogger(__name__)


@dataclass
class CallContext:
    """Metadata for an in-flight call, indexed by call_id."""

    call_id: str
    doctor: Doctor
    user: User
    constraints: AvailabilityWindow
    outcome_event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: CallOutcome | None = None


# Global registry of in-flight calls.  Keyed by conversation_id
# (ElevenLabs' conversation identifier returned on the /ws session).
_active_calls: dict[str, CallContext] = {}

# Per-call state (simple approach since calls are sequential).
_active_specialty: str = ""
_active_patient_name: str = ""
_active_busy_intervals: list[str] = []


def set_active_specialty(conversation_id: str, specialty: str) -> None:
    global _active_specialty
    _active_specialty = specialty


def get_active_specialty() -> str:
    return _active_specialty


def set_patient_name(name: str) -> None:
    global _active_patient_name
    _active_patient_name = name


def get_patient_name() -> str:
    return _active_patient_name


def set_busy_intervals(intervals: list[str]) -> None:
    global _active_busy_intervals
    _active_busy_intervals = intervals


def get_busy_intervals() -> list[str]:
    return _active_busy_intervals


def register_call(call_id: str, ctx: CallContext) -> None:
    _active_calls[call_id] = ctx


def get_call_context(call_id: str) -> CallContext | None:
    return _active_calls.get(call_id)


def resolve_call(call_id: str, outcome: CallOutcome) -> None:
    """Set the outcome and signal the waiter."""
    ctx = _active_calls.pop(call_id, None)
    if ctx:
        ctx.outcome = outcome
        ctx.outcome_event.set()


def _parse_spanish_date(text: str) -> datetime:
    """Best-effort parse of a Spanish date string like 'martes 26 de mayo a las 10:00'.

    Falls back to datetime.utcnow() if parsing fails.
    """
    import re

    MONTHS = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }
    now = datetime.utcnow()

    # Extract day number
    day_match = re.search(r"\b(\d{1,2})\b", text)
    day = int(day_match.group(1)) if day_match else now.day

    # Extract month
    month = now.month
    lower = text.lower()
    for name, num in MONTHS.items():
        if name in lower:
            month = num
            break

    # Extract time (HH:MM)
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    hour, minute = (int(time_match.group(1)), int(time_match.group(2))) if time_match else (now.hour, now.minute)

    # Determine year
    year = now.year
    if month < now.month or (month == now.month and day < now.day):
        year += 1

    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        logger.warning("Failed to parse Spanish date '%s', using now", text)
        return now


def resolve_active_if_pending(
    *,
    reason: str | None = None,
    success: bool = False,
    date_hint: str = "",
) -> None:
    """Resolve the single active call (calls are sequential).

    Called from the chat-completions stream when CITA_CONFIRMADA or
    SIN_DISPONIBILIDAD is detected, or from the Twilio status callback
    when the call ends.
    """
    if not _active_calls:
        return
    call_id, ctx = next(iter(_active_calls.items()))
    if ctx.outcome_event.is_set():
        return  # already resolved

    from domain.value_objects.time_slot import TimeSlot

    slot = None
    if success:
        parsed = _parse_spanish_date(date_hint)
        slot = TimeSlot(start=parsed, duration_minutes=30)
        logger.info(
            "Resolving call %s as SUCCESS (date_hint=%s → %s)",
            call_id,
            date_hint,
            parsed.isoformat(),
        )
    else:
        logger.info("Resolving call %s as FAILED (reason=%s)", call_id, reason)

    outcome = CallOutcome(
        doctor=ctx.doctor,
        success=success,
        slot=slot,
        reason=reason,
    )
    resolve_call(call_id, outcome)


async def run_call_session(
    *,
    config: "VoiceCallerConfig",  # noqa: F821
    doctor: Doctor,
    user: User,
    constraints: AvailabilityWindow,
    to_phone: str,
) -> CallOutcome:
    """Place an outbound call via ElevenLabs and wait for completion.

    ElevenLabs handles the Twilio audio bridge natively.  When the call
    connects, ElevenLabs opens a WebSocket to our /ws endpoint where we
    run Claude for the booking conversation.
    """
    from elevenlabs import ElevenLabs

    from infrastructure.voice.elevenlabs_voice_caller import VoiceCallerConfig

    cfg: VoiceCallerConfig = config

    call_id = str(uuid.uuid4())
    ctx = CallContext(
        call_id=call_id,
        doctor=doctor,
        user=user,
        constraints=constraints,
    )
    register_call(call_id, ctx)

    try:
        client = ElevenLabs(api_key=cfg.elevenlabs_api_key)
        specialty_name = doctor.specialty.name
        response = client.conversational_ai.twilio.outbound_call(
            agent_id=cfg.elevenlabs_agent_id,
            agent_phone_number_id=cfg.elevenlabs_phone_number_id,
            to_number=to_phone,
        )
        # Store specialty for the chat completions endpoint to look up
        conv_id = getattr(response, "conversation_id", None)
        if conv_id:
            set_active_specialty(conv_id, specialty_name)
        logger.info(
            "ElevenLabs outbound call placed: call_id=%s → %s (response=%s)",
            call_id,
            to_phone,
            response,
        )
    except Exception as exc:
        logger.exception("Failed to place outbound call to %s", to_phone)
        _active_calls.pop(call_id, None)
        return CallOutcome(doctor=doctor, success=False, reason=f"call_failed: {exc}")

    # Wait for the conversation to complete (resolved by the /ws handler).
    try:
        await asyncio.wait_for(ctx.outcome_event.wait(), timeout=120)
    except asyncio.TimeoutError:
        _active_calls.pop(call_id, None)
        return CallOutcome(doctor=doctor, success=False, reason="timeout")

    if ctx.outcome is None:
        return CallOutcome(doctor=doctor, success=False, reason="no_outcome")
    return ctx.outcome
