"""ElevenLabs Speech Engine WebSocket handler.

Endpoints:
  WS   /ws  — ElevenLabs connects here when a call starts. We receive
       transcripts and respond with Claude (Anthropic) for the booking
       conversation.
  POST /webhooks/voice/status — Twilio status callback (kept for graceful
       handling of call-level events like busy/no-answer).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def speech_engine_ws(websocket: WebSocket) -> None:
    """Handle ElevenLabs Speech Engine WebSocket connections.

    ElevenLabs connects here when a call is in progress. Each connection
    represents one conversation. We receive user transcripts and stream
    Claude's responses back via session.send_response().

    The SDK's SpeechEngineSession natively supports FastAPI WebSockets
    (it detects receive_text/send_text and wraps automatically).
    """
    await websocket.accept()

    from config import get_settings
    from elevenlabs.speech_engine.session import SpeechEngineSession

    settings = get_settings()

    session = SpeechEngineSession(websocket, debug=True)

    async def on_transcript(transcript: list) -> None:
        """Called each time the user finishes a turn."""
        import anthropic

        messages = []
        for msg in transcript:
            role = "assistant" if msg.role == "agent" else "user"
            messages.append({"role": role, "content": msg.content})

        logger.info(
            "Transcript (%d turns), last: %s",
            len(messages),
            messages[-1]["content"][:80] if messages else "",
        )

        system_prompt = _get_booking_system_prompt()

        try:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            stream = client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                system=system_prompt,
                messages=messages,
            )
            await session.send_response(stream)
        except Exception:
            logger.exception("Error in on_transcript handler")
            await session.send_response(
                "Lo siento, ha ocurrido un error. ¿Puede repetir?"
            )

    async def on_init(conversation_id: str) -> None:
        logger.info("Speech Engine session initialized: %s", conversation_id)

    session.on("user_transcript", on_transcript)
    session.on("init", on_init)

    try:
        await session.run()
    except Exception:
        logger.exception("Speech Engine session error")
    finally:
        logger.info("Speech Engine session ended")


@router.post("/webhooks/voice/status")
async def voice_status(request: Request) -> Response:
    """Handle Twilio call status callbacks."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_status = str(form.get("CallStatus", ""))

    logger.info("Call status update: sid=%s status=%s", call_sid, call_status)

    return Response(content="", status_code=204)


def _get_booking_system_prompt() -> str:
    return (
        "Eres un asistente de reservas médicas que llama a clínicas en España "
        "para agendar citas. Hablas en español de forma clara, profesional y "
        "cortés. Tu objetivo es:\n"
        "1. Saludar e identificarte como asistente del paciente.\n"
        "2. Preguntar por disponibilidad para la especialidad solicitada.\n"
        "3. Si hay disponibilidad, confirmar fecha y hora.\n"
        "4. Agradecer y despedirte.\n\n"
        "Si la recepcionista dice que no hay disponibilidad, agradece "
        "amablemente y despídete. Sé conciso y natural — estás al teléfono.\n\n"
        "IMPORTANTE: Cuando consigas confirmar una cita, incluye en tu "
        "última respuesta la palabra CITA_CONFIRMADA seguida de la fecha y "
        "hora. Ejemplo: 'Perfecto, CITA_CONFIRMADA martes 27 de mayo a las "
        "10:00. Muchas gracias.'\n\n"
        "Si no hay disponibilidad, di SIN DISPONIBILIDAD antes de despedirte."
    )
