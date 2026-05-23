"""Tests for `application.intent_parser.parse_appointment_intent`.

The parser runs once per inbound WhatsApp booking message and decides:
  - which specialty the user wants
  - how urgent it is (`max_weeks`, or None to fall back to the user's
    profile default)
  - returns None when nothing actionable can be extracted
"""
from __future__ import annotations

from application.intent_parser import parse_appointment_intent


# ---- specialty extraction ----

class TestSpecialtyExtraction:
    def test_direct_specialty_match(self):
        req = parse_appointment_intent("psicologo")
        assert req is not None
        assert req.specialty.name == "PSICOLOGIA"

    def test_specialty_inside_sentence(self):
        req = parse_appointment_intent("necesito un psicologo")
        assert req is not None
        assert req.specialty.name == "PSICOLOGIA"

    def test_specialty_with_punctuation(self):
        req = parse_appointment_intent("Hola, busco un dermatologo, gracias")
        assert req is not None
        # Cigna's canonical name carries the accent; matcher normalises.
        assert req.specialty.name == "DERMATOLOGÍA"

    def test_no_specialty_returns_none(self):
        """When the user types something the catalog doesn't recognise,
        we return None so the caller asks for clarification instead of
        guessing wrong."""
        assert parse_appointment_intent("hello there") is None
        assert parse_appointment_intent("") is None

    def test_raw_query_preserved(self):
        body = "Necesito un psicologo URGENTEMENTE, me duele todo"
        req = parse_appointment_intent(body)
        assert req is not None
        assert req.raw_query == body


# ---- urgency detection: explicit keywords ----

class TestExplicitUrgency:
    def test_spanish_urgente_marks_as_urgent(self):
        req = parse_appointment_intent("necesito un psicologo urgente")
        assert req is not None
        assert req.max_weeks == 1

    def test_dolor_marks_as_urgent(self):
        req = parse_appointment_intent("dermatologo, tengo dolor en la piel")
        assert req is not None
        assert req.max_weeks == 1

    def test_english_urgent_marks_as_urgent(self):
        req = parse_appointment_intent("psicologo, urgent please")
        assert req is not None
        assert req.max_weeks == 1

    def test_asap_marks_as_urgent(self):
        req = parse_appointment_intent("need a psicologo asap")
        assert req is not None
        assert req.max_weeks == 1

    def test_emergencia_marks_as_urgent(self):
        req = parse_appointment_intent("psicologo, emergencia")
        assert req is not None
        assert req.max_weeks == 1

    def test_routine_keyword_marks_as_non_urgent(self):
        req = parse_appointment_intent("dermatologo para chequeo")
        assert req is not None
        assert req.max_weeks == 6

    def test_english_routine_marks_as_non_urgent(self):
        req = parse_appointment_intent("dermatologo, routine checkup")
        assert req is not None
        assert req.max_weeks == 6

    def test_urgent_beats_non_urgent_when_both_present(self):
        """'I need a routine checkup but I'm in pain' — pain wins.
        The user's signal of distress overrides the framing."""
        req = parse_appointment_intent(
            "dermatologo, routine checkup but me duele mucho"
        )
        assert req is not None
        assert req.max_weeks == 1


# ---- urgency: high-risk specialty defaults ----

class TestHighRiskSpecialtyDefaults:
    def test_cardiology_defaults_to_urgent(self):
        req = parse_appointment_intent("cardiologia")
        assert req is not None
        assert req.max_weeks == 1

    def test_oncology_defaults_to_urgent(self):
        req = parse_appointment_intent("oncologia medica")
        assert req is not None
        assert req.max_weeks == 1

    def test_neurology_defaults_to_urgent(self):
        req = parse_appointment_intent("neurologia")
        assert req is not None
        assert req.max_weeks == 1

    def test_explicit_routine_overrides_high_risk_default(self):
        """If a user explicitly says it's a routine cardiology checkup
        (e.g. annual control), respect that. Explicit user signal beats
        the specialty-based heuristic."""
        req = parse_appointment_intent("cardiologia, chequeo rutinario")
        assert req is not None
        assert req.max_weeks == 6


# ---- no inference → leaves max_weeks unset for caller fallback ----

class TestNoInferenceFallsBack:
    def test_plain_specialty_leaves_max_weeks_none(self):
        """A bare 'psicologo' with no urgency keywords and no high-risk
        specialty leaves max_weeks=None. The caller (book use case) will
        use the user's profile default (`user.availability.max_weeks_out`)."""
        req = parse_appointment_intent("psicologo")
        assert req is not None
        assert req.max_weeks is None

    def test_dermatology_routine_phrasing_no_keyword(self):
        """Specialty without explicit urgency keywords and not high-risk."""
        req = parse_appointment_intent("dermatologo, sin prisa especial")
        assert req is not None
        # "sin prisa especial" is not in our keyword set — intentional;
        # we'd rather miss than match too aggressively.
        assert req.max_weeks is None
