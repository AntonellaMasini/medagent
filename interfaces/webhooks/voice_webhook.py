"""ElevenLabs Custom LLM endpoint + Twilio ↔ Speech Engine bridge.

Endpoints:
  POST /v1/chat/completions — OpenAI-compatible endpoint that ElevenLabs
       calls with conversation transcripts. We forward to Claude and stream
       back in OpenAI SSE format.
  WS   /ws  — Speech Engine WebSocket handler (server-side SDK transport).
  POST /twiml/outbound — returns TwiML that connects call audio to our bridge.
  WS   /media-stream — Twilio Media Stream ↔ ElevenLabs Conversation bridge.
  POST /webhooks/voice/status — Twilio status callback.
"""
from __future__ import annotations

import asyncio
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
        get_doctor_gender,
        get_insurance_id,
        get_insurer_name,
        get_max_weeks_out,
        get_patient_name,
        get_patient_phone,
    )

    specialty = get_active_specialty()
    busy_intervals = get_busy_intervals()
    patient_name = get_patient_name()
    max_weeks_out = get_max_weeks_out()
    doctor_gender = get_doctor_gender()
    insurer_name = get_insurer_name()
    insurance_id = get_insurance_id()
    patient_phone = get_patient_phone()

    # Always use our booking system prompt (ignore ElevenLabs' generic one)
    system_prompt = _get_booking_system_prompt(
        specialty, busy_intervals, patient_name, max_weeks_out, doctor_gender,
        insurer_name, insurance_id, patient_phone,
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
    """Handle ElevenLabs Speech Engine WebSocket connections."""
    from config import get_settings
    from elevenlabs.speech_engine.resource import verify_speech_engine_jwt
    from elevenlabs.speech_engine.session import SpeechEngineSession

    settings = get_settings()

    # Verify the JWT without an HTTP round-trip to ElevenLabs.
    auth_header = websocket.headers.get(
        "x-elevenlabs-speech-engine-authorization", "",
    )
    if auth_header:
        try:
            verify_speech_engine_jwt(auth_header, settings.elevenlabs_api_key)
        except ValueError:
            logger.warning("Speech Engine JWT verification failed — rejecting")
            await websocket.close(code=1008)
            return
    else:
        logger.warning("No Speech Engine auth header — rejecting")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    session = SpeechEngineSession(websocket, debug=True)

    async def on_transcript(transcript: list) -> None:
        import anthropic

        from infrastructure.voice.call_session import (
            get_active_specialty,
            get_busy_intervals,
            get_doctor_gender,
            get_insurance_id,
            get_insurer_name,
            get_max_weeks_out,
            get_patient_name,
            get_patient_phone,
        )

        messages = []
        for msg in transcript:
            role = "assistant" if msg.role == "agent" else "user"
            messages.append({"role": role, "content": msg.content})

        user_said = messages[-1]["content"] if messages else ""
        logger.info("\n🎙️  RECEPTIONIST: %s", user_said)
        logger.info("   [%d turns total]", len(messages))

        system_prompt = _get_booking_system_prompt(
            get_active_specialty(),
            get_busy_intervals(),
            get_patient_name(),
            get_max_weeks_out(),
            get_doctor_gender(),
            get_insurer_name(),
            get_insurance_id(),
            get_patient_phone(),
        )

        try:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

            async def _claude_stream():
                """Yield Claude text chunks; accumulate for outcome check."""
                accumulated = []
                async with client.messages.stream(
                    model="claude-sonnet-4-20250514",
                    max_tokens=300,
                    system=system_prompt,
                    messages=messages,
                ) as stream:
                    async for chunk in stream.text_stream:
                        accumulated.append(chunk)
                        yield chunk
                full_text = "".join(accumulated)
                logger.info("\n🤖 AGENTE: %s", full_text[:200])
                _check_booking_outcome(full_text)

            await session.send_response(_claude_stream())
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


@router.post("/twiml/outbound")
async def twiml_outbound(request: Request) -> Response:
    """Return TwiML that connects the outbound call to our media-stream bridge.

    Twilio POSTs here when the outbound call connects.  We respond with
    ``<Connect><Stream>`` pointing at our ``/media-stream`` WebSocket so
    Twilio pipes the call audio to us for Speech Engine processing.
    """
    from config import get_settings

    settings = get_settings()
    # Build the WebSocket URL from our public base_url
    ws_base = settings.base_url.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{ws_base}/media-stream"

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{stream_url}" />'
        "</Connect>"
        "</Response>"
    )
    logger.info("TwiML outbound: stream_url=%s", stream_url)
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/media-stream")
async def media_stream_bridge(websocket: WebSocket) -> None:
    """Bridge Twilio Media Stream audio ↔ ElevenLabs Conversation (Speech Engine).

    Twilio connects here after our TwiML ``<Connect><Stream>`` instruction.
    We create an ElevenLabs ``Conversation`` with a ``TwilioAudioInterface``
    that transcodes mulaw 8 kHz ↔ PCM 16 kHz and forward audio bidirectionally.
    """
    await websocket.accept()

    from config import get_settings
    from elevenlabs import ElevenLabs
    from elevenlabs.conversational_ai.conversation import (
        Conversation,
        ConversationInitiationData,
    )

    from infrastructure.voice.call_session import (
        get_active_specialty,
        get_doctor_gender,
        get_patient_name,
    )
    from infrastructure.voice.twilio_audio_interface import TwilioAudioInterface

    settings = get_settings()
    iface = TwilioAudioInterface()

    raw_specialty = _gendered_specialty(
        get_active_specialty(), get_doctor_gender(),
    )
    if raw_specialty:
        article = "una" if raw_specialty.endswith("a") else "un"
        specialty_text = f"{article} {raw_specialty}"
    else:
        specialty_text = "un especialista"

    el_client = ElevenLabs(api_key=settings.elevenlabs_api_key)
    conversation = Conversation(
        el_client,
        settings.elevenlabs_agent_id,
        requires_auth=True,
        audio_interface=iface,
        config=ConversationInitiationData(
            dynamic_variables={
                "patient_name": get_patient_name() or "el paciente",
                "specialty_text": specialty_text,
            },
        ),
        callback_agent_response=lambda resp: logger.info(
            "\n🔊 AGENT→TWILIO: %s", resp[:200] if resp else ""
        ),
        callback_user_transcript=lambda txt: logger.info(
            "\n🎙️  TWILIO→AGENT: %s", txt[:200] if txt else ""
        ),
    )

    logger.info("Starting ElevenLabs Conversation for media-stream bridge")
    conversation.start_session()

    try:
        # Pump loop: read Twilio messages and forward audio to ElevenLabs,
        # while also draining ElevenLabs audio back to Twilio.
        async def _pump_twilio_to_el() -> None:
            """Read Twilio WebSocket messages and feed audio into the interface."""
            try:
                while True:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    event = msg.get("event", "")
                    if event == "start":
                        stream_sid = msg["start"]["streamSid"]
                        iface.set_stream_sid(stream_sid)
                        logger.info("Twilio stream started: sid=%s", stream_sid)
                    elif event == "media":
                        iface.receive_twilio_audio(msg["media"]["payload"])
                    elif event == "stop":
                        logger.info("Twilio stream stopped")
                        return
            except Exception:
                logger.debug("Twilio WS read ended", exc_info=True)

        async def _pump_el_to_twilio() -> None:
            """Drain ElevenLabs audio and send to Twilio."""
            try:
                while True:
                    msgs = iface.drain_output()
                    for m in msgs:
                        await websocket.send_text(m)
                    await asyncio.sleep(0.02)  # ~50 fps drain rate
            except Exception:
                logger.debug("Twilio WS write ended", exc_info=True)

        await asyncio.gather(
            _pump_twilio_to_el(),
            _pump_el_to_twilio(),
            return_exceptions=True,
        )
    except Exception:
        logger.exception("Media stream bridge error")
    finally:
        conversation.end_session()
        conversation.wait_for_session_end()
        logger.info("Media stream bridge ended")


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


# Map from canonical Cigna specialty name to (feminine, masculine) forms
# for use in the voice prompt.  Only specialties likely to appear in the
# hackathon demo need entries here; others fall back to the raw name.
_GENDERED_SPECIALTIES: dict[str, tuple[str, str]] = {
    "PSICOLOGIA": ("psicóloga", "psicólogo"),
    "DERMATOLOGÍA": ("dermatóloga", "dermatólogo"),
    "CARDIOLOGÍA": ("cardióloga", "cardiólogo"),
    "NEUROLOGÍA": ("neuróloga", "neurólogo"),
    "OBSTETRICIA Y GINECOLOGÍA": ("ginecóloga", "ginecólogo"),
    "PEDIATRÍA": ("pediatra", "pediatra"),
    "OFTALMOLOGÍA": ("oftalmóloga", "oftalmólogo"),
    "TRAUMATOLOGÍA": ("traumatóloga", "traumatólogo"),
    "ENDOCRINOLOGÍA": ("endocrinóloga", "endocrinólogo"),
    "OTORRINOLARINGOLOGÍA": ("otorrinolaringóloga", "otorrinolaringólogo"),
}


_LETTER_NAMES: dict[str, str] = {
    "A": "a", "B": "be", "C": "ce", "D": "de", "E": "e",
    "F": "efe", "G": "ge", "H": "hache", "I": "i", "J": "jota",
    "K": "ka", "L": "ele", "M": "eme", "N": "ene", "O": "o",
    "P": "pe", "Q": "cu", "R": "erre", "S": "ese", "T": "te",
    "U": "u", "V": "uve", "W": "uve doble", "X": "equis",
    "Y": "i griega", "Z": "zeta",
}


def _spell_out_for_tts(value: str) -> str:
    """Convert an ID or phone number into a TTS-friendly spelled-out form.

    Examples:
      "Z3512875K01" → "zeta, tres, cinco, uno, dos, ocho, siete, cinco, ka, cero, uno"
      "+34612345678" → "tres, cuatro, seis, uno, dos, tres, cuatro, cinco, seis, siete, ocho"
    """
    parts: list[str] = []
    for ch in value:
        upper = ch.upper()
        if upper in _LETTER_NAMES:
            parts.append(_LETTER_NAMES[upper])
        elif ch.isdigit():
            digit_names = {
                "0": "cero", "1": "uno", "2": "dos", "3": "tres",
                "4": "cuatro", "5": "cinco", "6": "seis", "7": "siete",
                "8": "ocho", "9": "nueve",
            }
            parts.append(digit_names[ch])
        # Skip +, -, spaces, etc.
    return ", ".join(parts)


def _gendered_specialty(specialty: str, gender: str) -> str:
    """Return the gendered form of a specialty name for the voice prompt."""
    pair = _GENDERED_SPECIALTIES.get(specialty.upper())
    if pair is None:
        return specialty.lower()
    return pair[0] if gender == "female" else pair[1]


def _get_booking_system_prompt(
    specialty: str = "",
    busy_intervals: list[str] | None = None,
    patient_name: str = "",
    max_weeks_out: int = 4,
    doctor_gender: str = "",
    insurer_name: str = "",
    insurance_id: str = "",
    patient_phone: str = "",
) -> str:
    # The first_message template (set in setup_speech_engine.py) already
    # greets and states the reason for calling, so Claude must NOT repeat it.
    if patient_name:
        greeting = (
            f"YA te has presentado al inicio de la llamada diciendo tu nombre "
            f"y que llamas de parte de {patient_name}. "
            f"NO vuelvas a presentarte ni a saludar — la recepcionista ya "
            f"sabe quién eres y por qué llamas."
        )
    else:
        greeting = (
            "YA te has presentado al inicio de la llamada. "
            "NO vuelvas a saludar ni a presentarte."
        )

    # Build specialty + gender lines
    gender_line = ""
    if doctor_gender and specialty:
        gendered = _gendered_specialty(specialty, doctor_gender)
        article = "una" if doctor_gender == "female" else "un"
        gender_word = "mujer" if doctor_gender == "female" else "hombre"
        specialty_line = (
            f"Pide cita con {article} {gendered} {gender_word}. "
            f"SIEMPRE di '{article} {gendered} {gender_word}' — "
            f"nunca digas solo '{specialty.lower()}' sin especificar "
            f"el género.\n"
        )
        gender_line = (
            f"\n\n*** PREFERENCIA DE GÉNERO (OBLIGATORIO) ***\n"
            f"El paciente quiere un doctor que sea {gender_word}. "
            f"Debes decir explícitamente '{article} {gendered} {gender_word}' "
            f"a la recepcionista. Si la recepcionista ofrece un doctor "
            f"del género contrario, rechaza amablemente y pide "
            f"específicamente {article} {gendered} {gender_word}. "
            f"Al confirmar la cita, verifica que el doctor sea {gender_word}.\n"
        )
    elif specialty:
        specialty_line = f"La especialidad es: {specialty}.\n"
    else:
        specialty_line = ""

    busy_line = ""
    if busy_intervals:
        busy_list = "; ".join(busy_intervals[:20])
        busy_line = (
            f"\n\n*** HORARIOS NO DISPONIBLES (OBLIGATORIO) ***\n"
            f"El paciente tiene compromisos en estos horarios: {busy_list}.\n"
            f"Necesita al menos 45 minutos de margen antes y después de cada "
            f"compromiso. NO aceptes citas que se solapen con estos horarios "
            f"o que caigan dentro del margen de 45 minutos. Si te ofrecen un "
            f"horario que choca, di que ese horario no le viene bien y pide "
            f"otra opción.\n"
        )
    weeks_line = (
        f"\n\nPLAZO MÁXIMO: La cita debe ser dentro de las próximas "
        f"{max_weeks_out} semanas. Si solo ofrecen fechas más lejanas, "
        f"rechaza amablemente y di SIN_DISPONIBILIDAD.\n"
    )

    insurance_line = ""
    if insurer_name:
        insurance_line = (
            f"\n\n*** SEGURO MÉDICO (OBLIGATORIO) ***\n"
            f"El paciente tiene seguro privado con {insurer_name}. "
            f"Menciona que el paciente es asegurado de {insurer_name} "
            f"al principio de la conversación para que la recepcionista "
            f"lo tenga en cuenta.\n"
        )
        if patient_phone:
            spelled_phone = _spell_out_for_tts(patient_phone)
            insurance_line += (
                f"Si la recepcionista pide 'un número', 'un teléfono', "
                f"'número de contacto' o simplemente 'número', "
                f"SIEMPRE da el teléfono del paciente dígito por dígito: "
                f"'{spelled_phone}'. "
                f"'Número' sin más contexto = teléfono.\n"
            )
        if insurance_id:
            spelled_id = _spell_out_for_tts(insurance_id)
            insurance_line += (
                f"SOLO si la recepcionista pide específicamente el 'número "
                f"de asegurado', 'número de póliza', 'DNI', 'NIE' o "
                f"'documento de identidad', deletréalo letra por letra y "
                f"dígito por dígito: '{spelled_id}'. "
                f"No digas el código de golpe — deletréalo despacio.\n"
            )

    step2 = (
        f"2. Menciona que el paciente tiene seguro con {insurer_name}. "
        f"Pide disponibilidad. {specialty_line}"
        if insurer_name
        else f"2. Pide disponibilidad. {specialty_line}"
    )

    return (
        "Eres un asistente que llama a clínicas en España para agendar "
        "citas médicas EN NOMBRE DE UN PACIENTE. Hablas en español de "
        "forma clara, profesional y cortés. Tú eres QUIEN LLAMA.\n\n"
        f"IMPORTANTE: {greeting}\n\n"
        "Pasos de la conversación:\n"
        f"1. Cuando la recepcionista conteste, ve directo al grano. {step2}"
        "2. Si hay disponibilidad, confirma fecha y hora.\n"
        f"3. Da el nombre del paciente{f' ({patient_name})' if patient_name else ''} "
        "para que registren la cita.\n"
        "4. Agradece y despídete.\n\n"
        "Sé conciso y natural — estás al teléfono. Responde con frases "
        "cortas, no con párrafos.\n\n"
        "RESULTADO: Cuando confirmes una cita, incluye CITA_CONFIRMADA "
        "seguido de la fecha y hora. Ejemplo: 'Perfecto, CITA_CONFIRMADA "
        "martes 27 de mayo a las 10:00. Muchas gracias.'\n"
        "Si no hay disponibilidad, di SIN_DISPONIBILIDAD antes de despedirte."
        + insurance_line
        + busy_line
        + weeks_line
        + gender_line
    )
