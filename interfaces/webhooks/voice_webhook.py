"""ElevenLabs Custom LLM endpoint + legacy handlers.

Endpoints:
  POST /v1/chat/completions — OpenAI-compatible endpoint that ElevenLabs
       calls with conversation transcripts. We forward to Claude and stream
       back in OpenAI SSE format.
  WS   /ws  — Speech Engine WebSocket handler (alternative transport).
  POST /webhooks/voice/status — Twilio status callback.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> StreamingResponse:
    """OpenAI-compatible Chat Completions endpoint for ElevenLabs Custom LLM.

    ElevenLabs sends the conversation transcript in OpenAI format.
    We forward to Claude (Anthropic) and stream back in OpenAI SSE format.
    """
    import anthropic

    from config import get_settings

    settings = get_settings()
    body = await request.json()

    messages = body.get("messages", [])
    stream_requested = body.get("stream", False)

    # Get per-call state from the active call session
    from infrastructure.voice.call_session import (
        get_active_specialty,
        get_busy_intervals,
        get_max_weeks_out,
        get_patient_name,
    )

    specialty = get_active_specialty()
    busy_intervals = get_busy_intervals()
    patient_name = get_patient_name()
    max_weeks_out = get_max_weeks_out()

    # Always use our booking system prompt (ignore ElevenLabs' generic one)
    system_prompt = _get_booking_system_prompt(
        specialty, busy_intervals, patient_name, max_weeks_out
    )
    conversation_messages = []
    for msg in messages:
        if msg["role"] == "system":
            continue  # Skip — we use our own booking prompt
        else:
            conversation_messages.append(
                {"role": msg["role"], "content": msg["content"]}
            )

    # Ensure messages alternate correctly for Claude
    if not conversation_messages:
        conversation_messages = [{"role": "user", "content": "Hola"}]

    logger.info(
        "Chat completions request (%d messages, stream=%s)",
        len(conversation_messages),
        stream_requested,
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    if stream_requested:
        return StreamingResponse(
            _stream_claude_as_openai(client, system_prompt, conversation_messages),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    else:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=conversation_messages,
        )
        text = response.content[0].text
        return _make_openai_response(text)


async def _stream_claude_as_openai(
    client,
    system_prompt: str,
    messages: list[dict],
):
    """Stream Claude response formatted as OpenAI SSE chunks.

    Also accumulates the full response to detect booking keywords
    (CITA_CONFIRMADA / SIN_DISPONIBILIDAD) and resolve the active call.
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # Initial chunk with role
    initial_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "claude-sonnet-4-20250514",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(initial_chunk)}\n\n"

    accumulated_text = ""
    async with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system_prompt,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            accumulated_text += text
            chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "claude-sonnet-4-20250514",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"

    # Check for booking outcome keywords and resolve the active call
    _check_booking_outcome(accumulated_text)

    # Final chunk
    final_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "claude-sonnet-4-20250514",
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


def _make_openai_response(text: str) -> Response:
    """Format a non-streaming response in OpenAI format."""
    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "claude-sonnet-4-20250514",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return Response(
        content=json.dumps(body),
        media_type="application/json",
    )


@router.websocket("/ws")
async def speech_engine_ws(websocket: WebSocket) -> None:
    """Handle ElevenLabs Speech Engine WebSocket connections (alternative)."""
    await websocket.accept()

    from config import get_settings
    from elevenlabs.speech_engine.session import SpeechEngineSession

    settings = get_settings()

    session = SpeechEngineSession(websocket, debug=True)

    async def on_transcript(transcript: list) -> None:
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
    """Handle Twilio call status callbacks.

    When the call ends (completed/busy/no-answer/failed), resolve the
    active call if it hasn't been resolved yet — this prevents the
    timeout from firing and re-dialling the next doctor.

    For ``completed`` calls we wait a few seconds before resolving as
    failure — CITA_CONFIRMADA may still be detected in a streaming
    response that hasn't finished yet.
    """
    import asyncio

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_status = str(form.get("CallStatus", ""))

    logger.info("Call status update: sid=%s status=%s", call_sid, call_status)

    if call_status in ("busy", "no-answer", "failed", "canceled"):
        from infrastructure.voice.call_session import resolve_active_if_pending

        resolve_active_if_pending(reason=f"call_{call_status}")
    elif call_status == "completed":
        # Give the last streaming response time to detect CITA_CONFIRMADA
        await asyncio.sleep(3)
        from infrastructure.voice.call_session import resolve_active_if_pending

        resolve_active_if_pending(reason="call_completed")

    return Response(content="", status_code=204)


def _check_booking_outcome(text: str) -> None:
    """Detect CITA_CONFIRMADA / SIN_DISPONIBILIDAD in Claude's response
    and resolve the active call so the booking loop knows immediately."""
    from infrastructure.voice.call_session import resolve_active_if_pending

    upper = text.upper()
    if "CITA_CONFIRMADA" in upper:
        logger.info("Booking keyword CITA_CONFIRMADA detected in response")
        # Extract date/time text after the keyword for logging
        idx = upper.index("CITA_CONFIRMADA")
        date_hint = text[idx + len("CITA_CONFIRMADA"):].strip()[:80]
        resolve_active_if_pending(
            reason=None,
            success=True,
            date_hint=date_hint,
        )
    elif "SIN DISPONIBILIDAD" in upper or "SIN_DISPONIBILIDAD" in upper:
        logger.info("Booking keyword SIN_DISPONIBILIDAD detected in response")
        resolve_active_if_pending(reason="no_availability")


def _get_booking_system_prompt(
    specialty: str = "",
    busy_intervals: list[str] | None = None,
    patient_name: str = "",
    max_weeks_out: int = 4,
) -> str:
    specialty_line = (
        f"La especialidad que necesitas es: {specialty}.\n"
        if specialty
        else ""
    )
    patient_line = (
        f"El nombre del paciente es: {patient_name}.\n"
        if patient_name
        else ""
    )
    busy_line = ""
    if busy_intervals:
        busy_list = "; ".join(busy_intervals[:20])
        busy_line = (
            f"\n\nIMPORTANTE — HORARIOS NO DISPONIBLES DEL PACIENTE:\n"
            f"El paciente tiene compromisos en estos horarios: {busy_list}.\n"
            f"Necesita al menos 45 minutos de margen antes y después de cada "
            f"compromiso. NO aceptes citas que caigan en estos horarios o dentro "
            f"del margen de 45 minutos.\n"
        )
    weeks_line = (
        f"\n\nIMPORTANTE — PLAZO MÁXIMO:\n"
        f"La cita debe ser dentro de las próximas {max_weeks_out} semanas. "
        f"Si solo ofrecen fechas más lejanas, rechaza amablemente y di "
        f"SIN_DISPONIBILIDAD.\n"
    )
    return (
        "Eres un asistente de reservas médicas que llama a clínicas en España "
        "para agendar citas EN NOMBRE DE UN PACIENTE. Hablas en español de "
        "forma clara, profesional y cortés. Tú eres QUIEN LLAMA, no la "
        "recepcionista. Tu objetivo es:\n"
        f"1. Identificarte como asistente del paciente. {patient_line}"
        f"2. Pedir disponibilidad para la especialidad. {specialty_line}"
        "3. Si hay disponibilidad, confirmar fecha y hora.\n"
        "4. Dar el nombre completo del paciente para que la clínica registre la cita.\n"
        "5. Agradecer y despedirte.\n\n"
        "Si la recepcionista dice que no hay disponibilidad, agradece "
        "amablemente y despídete. Sé conciso y natural — estás al teléfono.\n\n"
        "IMPORTANTE: Cuando consigas confirmar una cita, incluye en tu "
        "última respuesta la palabra CITA_CONFIRMADA seguida de la fecha y "
        "hora. Ejemplo: 'Perfecto, CITA_CONFIRMADA martes 27 de mayo a las "
        "10:00. Muchas gracias.'\n\n"
        "Si no hay disponibilidad, di SIN_DISPONIBILIDAD antes de despedirte."
        + busy_line
        + weeks_line
    )
