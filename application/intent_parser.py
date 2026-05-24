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

# Specialties that imply urgency by default — when the user didn't say.
# Explicit allow-list rather than a substring match: "CARDIO"/"ONCO"/"NEURO"
# would catch BRONCOSCOPIA (br-ONCO-scopia), TRONCO CEREBRAL (tr-ONCO),
# NEUROPSICOLOGÍA, ECOCARDIOGRAMA, etc., none of which warrant a 1-week
# default. Compared against `Specialty.name` (Cigna's canonical
# language_ES value); keep the accents — they're part of the canonical
# string.
_HIGH_RISK_SPECIALTY_NAMES: frozenset[str] = frozenset({
    # Cardiology
    "CARDIOLOGÍA",
    "CARDIOLOGÍA INFANTIL",
    "CARDIOLOGÍA INTERVENCIONISTA",
    "CIRUGÍA CARDIOVASCULAR",
    "UAR CARDIOLOGÍA",
    # Oncology
    "ONCOLOGÍA MÉDICA",
    "ONCOLOGÍA MÉDICA INFANTIL",
    "ONCOLOGÍA RADIOTERÁPICA",
    # Neurology / neurosurgery (the clinically-urgent ones — explicitly
    # excludes NEUROFISIOLOGÍA, NEUROPSICOLOGÍA, NUTRICIÓN PATOLOGÍA
    # NEURODEGENERATIVA, etc.)
    "NEUROLOGÍA",
    "NEUROLOGÍA INFANTIL",
    "NEUROCIRUGÍA",
    "NEUROCIRUGÍA INFANTIL",
})

# How many weeks out we'll accept a slot when the message explicitly
# signals urgency. Tuned against typical Spanish private-insurance wait
# times; revisit if real bookings show this is wrong.
_URGENT_MAX_WEEKS = 1


def parse_appointment_intent(text: str) -> AppointmentRequest | None:
    """Build an AppointmentRequest from a free-text booking message.

    Returns None when no specialty can be extracted — caller should reply
    asking the user to clarify what they need. (We deliberately do not
    invent a default specialty: the wrong one is worse than asking.)

    `max_weeks` resolution order:

      1. Explicit urgent keywords ("urgente", "dolor", "asap", …)
         → 1 week. Tighter than the user's profile default.

      2. Explicit non-urgent keywords ("rutina", "chequeo", "checkup", …)
         → None (use user's profile default), AND short-circuit the
         high-risk specialty heuristic below. The user told us it's
         routine; we shouldn't override that with a CARDIO/ONCO/NEURO
         default, and we also shouldn't *widen* their booking window
         past their own configured default (a previous version set
         this to 6 weeks, which was presumptuous — `max_weeks_out=4`
         is the project-wide default and most users will have just
         that on their profile).

      3. No explicit signal + high-risk specialty (cardio / onco / neuro
         from the allow-list) → 1 week.

      4. Otherwise → None. Caller falls back to
         `user.availability.max_weeks_out`.
    """
    specialty = _extract_specialty(text)
    if specialty is None:
        return None

    if _is_explicitly_urgent(text):
        max_weeks: int | None = _URGENT_MAX_WEEKS
    elif _is_explicitly_non_urgent(text):
        # User said it's routine — respect that and use their normal
        # profile default. Skip the high-risk specialty override below.
        max_weeks = None
    else:
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


def _is_explicitly_urgent(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in _URGENT_KEYWORDS)


def _is_explicitly_non_urgent(text: str) -> bool:
    """True if the user's message contains a routine-care signal.

    Note: only checked when `_is_explicitly_urgent` is already False,
    so we don't need to worry about the "routine checkup but in pain"
    case — urgent wins by being checked first.
    """
    lower = text.lower()
    return any(keyword in lower for keyword in _NON_URGENT_KEYWORDS)


def _high_risk_specialty_default(specialty: Specialty) -> int | None:
    if specialty.name in _HIGH_RISK_SPECIALTY_NAMES:
        return _URGENT_MAX_WEEKS
    return None


__all__ = ["parse_appointment_intent"]
