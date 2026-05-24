"""Single-call session: places call via Twilio and tracks the outcome.

This module initiates the Twilio outbound call and stores per-call
metadata so the voice webhook can look up context when Twilio connects
the media stream.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from application.ports import CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import AvailabilityWindow

logger = logging.getLogger(__name__)


@dataclass
class CallContext:
    """Metadata for an in-flight call, indexed by call_sid."""

    call_id: str
    doctor: Doctor
    user: User
    constraints: AvailabilityWindow
    outcome_event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: CallOutcome | None = None


# Global registry of in-flight calls.  Keyed by call_sid (Twilio's call
# identifier).  The voice webhook reads context from here when the media
# stream connects.
_active_calls: dict[str, CallContext] = {}


def register_call(call_sid: str, ctx: CallContext) -> None:
    _active_calls[call_sid] = ctx


def get_call_context(call_sid: str) -> CallContext | None:
    return _active_calls.get(call_sid)


def resolve_call(call_sid: str, outcome: CallOutcome) -> None:
    """Set the outcome and signal the waiter."""
    ctx = _active_calls.pop(call_sid, None)
    if ctx:
        ctx.outcome = outcome
        ctx.outcome_event.set()


async def run_call_session(
    *,
    config: "VoiceCallerConfig",  # noqa: F821
    doctor: Doctor,
    user: User,
    constraints: AvailabilityWindow,
    to_phone: str,
) -> CallOutcome:
    """Place the Twilio call and wait for the conversation to complete."""
    from twilio.rest import Client as TwilioClient

    from infrastructure.voice.elevenlabs_voice_caller import VoiceCallerConfig

    cfg: VoiceCallerConfig = config

    twilio_client = TwilioClient(cfg.twilio_account_sid, cfg.twilio_auth_token)

    call_id = str(uuid.uuid4())
    ctx = CallContext(
        call_id=call_id,
        doctor=doctor,
        user=user,
        constraints=constraints,
    )

    # The outbound TwiML URL tells Twilio to stream audio to our WS endpoint.
    twiml_url = cfg.base_url_ws.rstrip("/").replace("wss://", "https://").replace(
        "ws://", "http://"
    ) + "/webhooks/voice/outbound"

    # Place the call.  Twilio fetches the TwiML from our endpoint.
    call = twilio_client.calls.create(
        to=to_phone,
        from_=cfg.twilio_voice_number,
        url=twiml_url,
        timeout=30,
        status_callback=cfg.base_url_ws.rstrip("/").replace("wss://", "https://").replace(
            "ws://", "http://"
        ) + "/webhooks/voice/status",
        status_callback_event=["completed", "no-answer", "busy", "failed"],
    )

    logger.info("Twilio call placed: SID=%s → %s", call.sid, to_phone)
    register_call(call.sid, ctx)

    # Wait for the conversation to complete (set by the webhook).
    await ctx.outcome_event.wait()

    if ctx.outcome is None:
        return CallOutcome(doctor=doctor, success=False, reason="no_outcome")
    return ctx.outcome
