"""Twilio Voice webhooks: TwiML for outbound calls + Media Stream WebSocket.

Endpoints:
  GET  /webhooks/voice/outbound — Returns TwiML instructing Twilio to open a
       bidirectional Media Stream WebSocket to our server.
  WS   /webhooks/voice/stream   — Receives Twilio Media Stream events (start,
       media, stop) and bridges audio to/from ElevenLabs Speech Engine.
  POST /webhooks/voice/status   — Receives call status callbacks from Twilio.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response

from infrastructure.voice.call_session import (
    CallContext,
    get_call_context,
    resolve_call,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/voice", tags=["voice"])


@router.api_route("/outbound", methods=["GET", "POST"])
async def voice_outbound(request: Request) -> Response:
    """Return TwiML that opens a bidirectional Media Stream.

    Twilio fetches this when the outbound call connects.  The TwiML tells
    Twilio to stream audio to our /webhooks/voice/stream WebSocket.
    """
    from config import get_settings

    settings = get_settings()
    ws_url = settings.base_url_ws.rstrip("/")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}/webhooks/voice/stream" />
    </Connect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@router.websocket("/stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Handle the Twilio Media Stream WebSocket.

    Twilio sends JSON frames with events: connected, start, media, stop.
    We bridge audio to ElevenLabs Speech Engine via the Conversation class.
    """
    await websocket.accept()
    logger.info("Twilio Media Stream WebSocket connected")

    call_sid: str | None = None
    stream_sid: str | None = None
    call_ctx: CallContext | None = None

    try:
        async for raw_message in websocket.iter_text():
            msg = json.loads(raw_message)
            event = msg.get("event")

            if event == "connected":
                logger.debug("Media stream connected: %s", msg)

            elif event == "start":
                start_data = msg.get("start", {})
                call_sid = start_data.get("callSid")
                stream_sid = start_data.get("streamSid")
                logger.info(
                    "Media stream started: call_sid=%s stream_sid=%s",
                    call_sid,
                    stream_sid,
                )
                if call_sid:
                    call_ctx = get_call_context(call_sid)
                    if call_ctx:
                        # Start the Speech Engine conversation in background
                        asyncio.create_task(
                            _run_speech_engine_conversation(
                                websocket=websocket,
                                call_ctx=call_ctx,
                                stream_sid=stream_sid,
                            )
                        )

            elif event == "media":
                # Audio data from the caller (the receptionist's voice).
                # In the full implementation, this gets forwarded to
                # ElevenLabs for STT.  For now handled by the conversation
                # task started above.
                pass

            elif event == "stop":
                logger.info("Media stream stopped: call_sid=%s", call_sid)
                break

    except Exception:
        logger.exception("Error in voice stream WebSocket")
    finally:
        logger.info("Voice stream WebSocket closed")


@router.post("/status")
async def voice_status(request: Request) -> Response:
    """Handle Twilio call status callbacks.

    When a call completes/fails without going through the conversation
    (e.g. no answer, busy), we resolve the call with a failure outcome.
    """
    from application.ports import CallOutcome

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_status = str(form.get("CallStatus", ""))

    logger.info("Call status update: sid=%s status=%s", call_sid, call_status)

    if call_status in ("no-answer", "busy", "failed", "canceled"):
        ctx = get_call_context(call_sid)
        if ctx:
            outcome = CallOutcome(
                doctor=ctx.doctor,
                success=False,
                reason=call_status,
            )
            resolve_call(call_sid, outcome)

    return Response(content="", status_code=204)


async def _run_speech_engine_conversation(
    *,
    websocket: WebSocket,
    call_ctx: CallContext,
    stream_sid: str | None,
) -> None:
    """Run the Speech Engine conversation for a connected call.

    This bridges the Twilio audio stream to ElevenLabs Speech Engine
    and uses Claude to conduct the booking conversation.

    TODO(hackathon-followup): Implement full bidirectional audio bridge
    using the ElevenLabs Conversation class + TwilioAudioInterface.
    Current implementation uses a simplified text-based flow as proof
    of concept while the full audio bridge is being integrated.
    """
    from application.ports import CallOutcome
    from config import get_settings
    from infrastructure.voice.speech_engine_handler import (
        build_booking_system_prompt,
        get_llm_response,
        parse_booking_outcome,
    )

    settings = get_settings()
    system_prompt = build_booking_system_prompt(
        user=call_ctx.user,
        doctor=call_ctx.doctor,
        constraints=call_ctx.constraints,
    )

    # For the MVP, we run a single-turn conversation to demonstrate
    # the Speech Engine integration.  The full implementation will use
    # the ElevenLabs Conversation class for real-time audio streaming.
    transcript: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                "Buenos días, ¿tienen disponibilidad para una cita de "
                f"{call_ctx.doctor.specialty.name}?"
            ),
        }
    ]

    try:
        response_text = await get_llm_response(
            api_key=settings.anthropic_api_key,
            system_prompt=system_prompt,
            transcript=transcript,
        )
        logger.info("LLM response: %s", response_text[:200])

        success, slot, reason = parse_booking_outcome(
            response_text, call_ctx.doctor
        )

        outcome = CallOutcome(
            doctor=call_ctx.doctor,
            success=success,
            slot=slot,
            reason=reason,
        )
    except Exception as exc:
        logger.exception("Speech Engine conversation failed")
        outcome = CallOutcome(
            doctor=call_ctx.doctor,
            success=False,
            reason=f"speech_engine_error: {exc}",
        )

    # Signal the waiter
    call_sid_key = next(
        (k for k, v in _get_active_calls().items() if v is call_ctx),
        None,
    )
    if call_sid_key:
        resolve_call(call_sid_key, outcome)


def _get_active_calls() -> dict:
    """Access the active calls registry (avoid circular import)."""
    from infrastructure.voice.call_session import _active_calls

    return _active_calls
