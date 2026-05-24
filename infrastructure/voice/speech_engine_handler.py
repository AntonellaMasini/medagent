"""Speech Engine handler: receives transcripts, runs Claude, sends text back.

This module implements the LLM logic for the booking conversation.
ElevenLabs Speech Engine handles STT/TTS/turn-taking; we handle:
  - System prompt construction (Spanish, appointment-booking context)
  - Streaming Claude responses back to Speech Engine
  - Detecting when a booking is confirmed or failed
  - Extracting the booked time slot from the conversation
"""
from __future__ import annotations

import logging
from datetime import datetime

import anthropic

from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import AvailabilityWindow, TimeSlot

logger = logging.getLogger(__name__)

_BOOKING_SYSTEM_PROMPT = """\
Eres un asistente de voz que llama a una clínica médica en nombre de un \
paciente para reservar una cita.

**Datos del paciente:**
- Nombre: {user_name}
- Especialidad buscada: {specialty_name}

**Restricciones de disponibilidad:**
- Plazo máximo: {max_weeks} semanas desde hoy
- Preferencia horaria: {time_pref}

**Instrucciones:**
1. Saluda brevemente e identifícate: "Buenos días, llamo en nombre de \
{user_name} para solicitar una cita de {specialty_name}."
2. Pregunta por disponibilidad dentro del plazo indicado.
3. Si ofrecen un horario compatible, confírmalo. Repite fecha y hora para \
verificar.
4. Si no hay disponibilidad compatible, agradece y despídete.
5. Sé breve, cortés y profesional. No inventes información.
6. Cuando confirmes una cita, di exactamente: "CITA CONFIRMADA: [fecha] a \
las [hora]" para que el sistema pueda extraer la información.
7. Si no hay citas disponibles, di exactamente: "SIN DISPONIBILIDAD" antes \
de despedirte.

Habla siempre en español. No uses inglés.
"""

_RECEPTIONIST_SYSTEM_PROMPT = """\
Eres una recepcionista de una clínica médica en España. Tu clínica se llama \
"{clinic_name}" y ofrece la especialidad de {specialty_name}.

Cuando alguien te llame para pedir cita:
1. Saluda amablemente: "Clínica {clinic_name}, ¿en qué puedo ayudarle?"
2. Ofrece dos opciones de cita: martes a las 10:00 o jueves a las 16:00.
3. Si aceptan una opción, confírmala repitiendo fecha y hora.
4. Si ninguna les viene bien, discúlpate y di que no hay más disponibilidad \
esta semana.

Sé breve, natural y profesional. Habla siempre en español.
"""


def build_booking_system_prompt(
    user: User,
    doctor: Doctor,
    constraints: AvailabilityWindow,
) -> str:
    time_pref_map = {
        "mornings": "mañanas",
        "afternoons": "tardes",
        "any": "cualquier horario",
    }
    return _BOOKING_SYSTEM_PROMPT.format(
        user_name=user.name,
        specialty_name=doctor.specialty.name,
        max_weeks=constraints.max_weeks_out,
        time_pref=time_pref_map.get(constraints.preferred.value, "cualquier horario"),
    )


def build_receptionist_system_prompt(doctor: Doctor) -> str:
    return _RECEPTIONIST_SYSTEM_PROMPT.format(
        clinic_name=doctor.clinic_name,
        specialty_name=doctor.specialty.name,
    )


async def get_llm_response(
    *,
    api_key: str,
    system_prompt: str,
    transcript: list[dict[str, str]],
) -> str:
    """Run Claude on the transcript and return the full response text.

    For Speech Engine integration, this returns the complete response.
    The Speech Engine SDK handles streaming to the user via TTS.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)

    messages = []
    for msg in transcript:
        role = "assistant" if msg.get("role") == "agent" else "user"
        messages.append({"role": role, "content": msg["content"]})

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system_prompt,
        messages=messages,
    )

    return response.content[0].text


async def stream_llm_response(
    *,
    api_key: str,
    system_prompt: str,
    transcript: list[dict[str, str]],
):
    """Stream Claude response as an async generator of text chunks.

    Used by the Speech Engine WebSocket handler for real-time TTS.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)

    messages = []
    for msg in transcript:
        role = "assistant" if msg.get("role") == "agent" else "user"
        messages.append({"role": role, "content": msg["content"]})

    async with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system_prompt,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


def parse_booking_outcome(
    full_response: str,
    doctor: Doctor,
) -> tuple[bool, TimeSlot | None, str | None]:
    """Parse whether the conversation resulted in a booking.

    Returns (success, slot_or_none, reason_or_none).
    """
    upper = full_response.upper()

    if "CITA CONFIRMADA" in upper:
        # TODO(hackathon-followup): parse actual date/time from the response
        # For now, return a placeholder slot indicating success.
        slot = TimeSlot(start=datetime.now())
        return True, slot, None

    if "SIN DISPONIBILIDAD" in upper:
        return False, None, "no_availability"

    return False, None, "conversation_unclear"
