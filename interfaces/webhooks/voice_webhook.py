"""Twilio Voice webhooks: TwiML for outbound calls + Media Stream WebSocket.

Endpoints:
  GET  /webhooks/voice/outbound — Returns TwiML instructing Twilio to open a
       bidirectional Media Stream WebSocket to our server.
  WS   /webhooks/voice/stream   — Receives Twilio Media Stream events (start,
       media, stop) and bridges audio to/from ElevenLabs Speech Engine.
  POST /webhooks/voice/status   — Receives call status callbacks from Twilio.
"""
from __future__ import annotations

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
    We bridge audio to ElevenLabs Speech Engine via the Conversation class
    and TwilioAudioInterface.
    """
    await websocket.accept()
    logger.info("Twilio Media Stream WebSocket connected")

    from config import get_settings
    from elevenlabs import ElevenLabs
    from elevenlabs.conversational_ai.conversation import Conversation

    from infrastructure.voice.twilio_audio_interface import TwilioAudioInterface

    settings = get_settings()
    audio_interface = TwilioAudioInterface(websocket)
    conversation: Conversation | None = None
    call_sid: str | None = None
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
                logger.info(
                    "Media stream started: call_sid=%s stream_sid=%s",
                    call_sid,
                    start_data.get("streamSid"),
                )

                # Look up call context for this call
                if call_sid:
                    call_ctx = get_call_context(call_sid)

                # Forward start event to audio interface
                await audio_interface.handle_twilio_message(msg)

                # Start ElevenLabs conversation with Speech Engine
                if settings.elevenlabs_api_key and settings.elevenlabs_agent_id:
                    try:
                        client = ElevenLabs(api_key=settings.elevenlabs_api_key)
                        conversation = Conversation(
                            client=client,
                            agent_id=settings.elevenlabs_agent_id,
                            requires_auth=True,
                            audio_interface=audio_interface,
                            callback_agent_response=lambda text: logger.info(
                                "Agent said: %s", text[:100]
                            ),
                            callback_user_transcript=lambda text: logger.info(
                                "User said: %s", text[:100]
                            ),
                        )
                        conversation.start_session()
                        logger.info("ElevenLabs conversation started")
                    except Exception:
                        logger.exception(
                            "Failed to start ElevenLabs conversation"
                        )
                        conversation = None
                else:
                    logger.warning(
                        "ELEVENLABS_AGENT_ID not set — audio bridge disabled"
                    )

            elif event == "media":
                # Forward audio to ElevenLabs via the audio interface
                await audio_interface.handle_twilio_message(msg)

            elif event == "stop":
                logger.info("Media stream stopped: call_sid=%s", call_sid)
                break

    except Exception:
        logger.exception("Error in voice stream WebSocket")
    finally:
        # Clean up the ElevenLabs conversation
        if conversation:
            try:
                conversation.end_session()
                conversation.wait_for_session_end()
                logger.info("ElevenLabs conversation ended")
            except Exception:
                logger.exception("Error ending conversation session")

        # Resolve the call outcome if we have context
        if call_sid and call_ctx:
            _resolve_after_stream(call_sid, call_ctx)

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


def _resolve_after_stream(call_sid: str, call_ctx: CallContext) -> None:
    """Resolve the call after the media stream ends.

    When the stream closes normally (call hung up after conversation),
    we treat it as a completed call. The Speech Engine conversation
    has already run — if it detected a booking confirmation via the
    callback_agent_response, the outcome was set. Otherwise we resolve
    with a generic "stream_ended" reason so the caller doesn't hang.
    """
    from application.ports import CallOutcome

    if call_ctx.outcome is not None:
        return

    outcome = CallOutcome(
        doctor=call_ctx.doctor,
        success=False,
        reason="stream_ended_no_booking_detected",
    )
    resolve_call(call_sid, outcome)
