"""Parse a free-text booking request into a structured AppointmentRequest.

Runs once, in the WhatsApp webhook, before the booking flow kicks off. The
caller (the webhook) needs three pieces of information out of the user's
message:

  1. Which specialty does the user want? (`PSICOLOGIA`, `DERMATOLOGIA`, …)
  2. How urgent is it? (`max_weeks` — how far out we'll accept a slot)
  3. The original raw query, for context downstream.

Specialty extraction reuses the catalog matcher already used elsewhere
(`domain.value_objects.specialty.normalize_specialty`). Urgency is inferred
from keywords in the message; when neither explicit keywords nor a
high-risk specialty kicks in, `max_weeks` is left None and the use case
falls back to the user's profile default (`user.availability.max_weeks_out`).

Deliberately keyword-based, not LLM-based. Issue #15 tracks switching to
an LLM intent parser when the keyword set gets unwieldy. Until then the
rules here are easy to read, free, deterministic, and trivially testable.
"""
from __future__ import annotations

from application.book_appointment import AppointmentRequest
from domain.value_objects.specialty import Specialty, normalize_specialty


# Explicit urgency signals from the user. Substring match against the
# lowercased message body. Kept conservative — words that have common
# non-urgent uses ("ya", "no puedo", "control") are excluded to avoid
# false positives.
_URGENT_KEYWORDS: tuple[str, ...] = (
    # Spanish
    "urgente", "urgentemente", "urgencia", "emergencia",
    "lo antes posible", "cuanto antes", "lo mas pronto", "lo más pronto",
    "me duele", "duele", "dolor",
    # English
    "urgent", "urgently", "emergency", "asap", "pain", "hurts",
)

# Explicit non-urgent signals — user is doing routine care.
_NON_URGENT_KEYWORDS: tuple[str, ...] = (
    # Spanish
    "rutina", "rutinario", "rutinaria", "chequeo",
    # English
    "routine", "checkup", "check up", "check-up",
)

# Specialty-name substrings that imply urgency by default (when the user
# didn't explicitly say). Substring matched (case-insensitive) against
# Specialty.name. Covers CARDIOLOGÍA, CIRUGÍA CARDIOVASCULAR, ONCOLOGÍA
# MÉDICA, NEUROLOGÍA, NEUROCIRUGÍA, etc.
_HIGH_RISK_SPECIALTY_SUBSTRINGS: tuple[str, ...] = (
    "CARDIO",
    "ONCO",
    "NEURO",
)

# How many weeks out we'll accept a slot for each urgency level. Tuned
# against typical Spanish private-insurance wait times; revisit if real
# bookings show these are wrong.
_URGENT_MAX_WEEKS = 1
_NON_URGENT_MAX_WEEKS = 6


def parse_appointment_intent(text: str) -> AppointmentRequest | None:
    """Build an AppointmentRequest from a free-text booking message.

    Returns None when no specialty can be extracted — caller should reply
    asking the user to clarify what they need. (We deliberately do not
    invent a default specialty: the wrong one is worse than asking.)

    `max_weeks` resolution order, first hit wins:
      1. Explicit urgent keywords in the text → 1 week.
      2. Explicit non-urgent keywords ("rutina", "checkup", …) → 6 weeks.
      3. High-risk specialty default (cardio / onco / neuro) → 1 week.
      4. None → caller falls back to `user.availability.max_weeks_out`.
    """
    specialty = _extract_specialty(text)
    if specialty is None:
        return None

    max_weeks = _detect_urgency_from_text(text)
    if max_weeks is None:
        max_weeks = _high_risk_specialty_default(specialty)

    return AppointmentRequest(
        specialty=specialty,
        raw_query=text,
        max_weeks=max_weeks,
    )


# ---- internals ----


def _extract_specialty(text: str) -> Specialty | None:
    """First try the whole message as a specialty name, then scan tokens.

    Matches what the webhook previously did inline. Whole-message match
    first because users often just type the specialty by itself
    ("psicologo"); token scan is the fallback for sentences ("necesito un
    psicologo urgentemente").
    """
    direct = normalize_specialty(text)
    if direct is not None:
        return direct
    for token in text.replace(",", " ").split():
        match = normalize_specialty(token)
        if match is not None:
            return match
    return None


def _detect_urgency_from_text(text: str) -> int | None:
    """Return urgency-in-weeks if the user's message has an explicit
    signal, else None.

    Urgent keywords beat non-urgent ones — someone who writes "I need a
    routine checkup but I'm in pain" gets the urgent interpretation.
    """
    lower = text.lower()
    if any(keyword in lower for keyword in _URGENT_KEYWORDS):
        return _URGENT_MAX_WEEKS
    if any(keyword in lower for keyword in _NON_URGENT_KEYWORDS):
        return _NON_URGENT_MAX_WEEKS
    return None


def _high_risk_specialty_default(specialty: Specialty) -> int | None:
    name_upper = specialty.name.upper()
    if any(sub in name_upper for sub in _HIGH_RISK_SPECIALTY_SUBSTRINGS):
        return _URGENT_MAX_WEEKS
    return None


__all__ = ["parse_appointment_intent"]
