"""Tests for `application.intent_parser.parse_appointment_intent`.

The parser runs once per inbound WhatsApp booking message and decides:
  - which specialty the user wants
  - how urgent it is (`max_weeks`, or None to fall back to the user's
    profile default)
  - returns None when nothing actionable can be extracted
"""
from __future__ import annotations

from application.intent_parser import (
    _high_risk_specialty_default,
    parse_appointment_intent,
)
from domain.value_objects.specialty import Specialty, SpecialtyType


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

    def test_routine_keyword_falls_back_to_user_default(self):
        """Saying "it's routine" should leave max_weeks=None so the use case
        applies the user's profile default — NOT extend the booking window
        past it (a previous version returned 6 weeks here, which widened
        the search beyond the user's own 4-week default)."""
        req = parse_appointment_intent("dermatologo para chequeo")
        assert req is not None
        assert req.max_weeks is None

    def test_english_routine_falls_back_to_user_default(self):
        req = parse_appointment_intent("dermatologo, routine checkup")
        assert req is not None
        assert req.max_weeks is None

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
        (e.g. annual control), respect that. Non-urgent signal short-
        circuits the high-risk specialty override → max_weeks=None, so
        the user's profile default applies (NOT the 1-week cardio
        default that would otherwise kick in)."""
        req = parse_appointment_intent("cardiologia, chequeo rutinario")
        assert req is not None
        assert req.max_weeks is None

    def test_resolution_order_urgent_beats_high_risk_and_non_urgent(self):
        """Sanity: explicit 'urgent' beats everything else, even with a
        non-urgent keyword and a high-risk specialty in the same message."""
        req = parse_appointment_intent(
            "cardiologia chequeo rutinario pero me duele"
        )
        assert req is not None
        assert req.max_weeks == 1


# ---- English alias support (issue #24, hackathon demo) ----

class TestEnglishAliases:
    """English words should resolve to the correct Spanish specialty via
    the synonym dict. Covers the minimal demo set from issue #24."""

    def test_psychologist_maps_to_psicologia(self):
        req = parse_appointment_intent("book a psychologist")
        assert req is not None
        assert req.specialty.name == "PSICOLOGIA"

    def test_dermatologist_maps_to_dermatologia(self):
        req = parse_appointment_intent("I need a dermatologist")
        assert req is not None
        assert req.specialty.name == "DERMATOLOGÍA"

    def test_cardiologist_maps_to_cardiologia(self):
        req = parse_appointment_intent("cardiologist appointment please")
        assert req is not None
        assert req.specialty.name == "CARDIOLOGÍA"

    def test_gynecologist_maps_to_obstetricia(self):
        req = parse_appointment_intent("gynecologist")
        assert req is not None
        assert req.specialty.name == "OBSTETRICIA Y GINECOLOGÍA"


# ---- regression: substring false-positives that an earlier version had ----

def _spec(name: str) -> Specialty:
    """Build a Specialty for direct tests without going through the catalog
    matcher (which may not parse from arbitrary text)."""
    return Specialty(name=name, type=SpecialtyType.SPECIALTY)


class TestHighRiskFalsePositives:
    """Tests for specialties that LOOK like they'd match a CARDIO/ONCO/NEURO
    substring but aren't actually high-risk. An earlier version used
    `any(sub in name.upper() for sub in ("CARDIO","ONCO","NEURO"))` and
    silently tagged these as urgent, which made it harder to schedule
    slots when the user hadn't expressed any urgency.
    """

    def test_broncoscopia_is_not_high_risk(self):
        """'BR-ONCO-SCOPIA' contains the substring ONCO but is a lung
        endoscopy, not cancer."""
        assert _high_risk_specialty_default(_spec("BRONCOSCOPIA")) is None

    def test_tronco_cerebral_is_not_high_risk(self):
        """'POTENCIALES EVOCADOS TR-ONCO CEREBRAL' contains ONCO but is
        an evoked-potentials test."""
        assert _high_risk_specialty_default(
            _spec("POTENCIALES EVOCADOS TRONCO CEREBRAL")
        ) is None

    def test_neuropsicologia_is_not_high_risk(self):
        """Neuropsychology is a psychology specialty, not urgent."""
        assert _high_risk_specialty_default(_spec("NEUROPSICOLOGÍA")) is None

    def test_neurofisiologia_clinica_is_not_high_risk(self):
        """Neurophysiology is a diagnostic specialty, not urgent."""
        assert _high_risk_specialty_default(
            _spec("NEUROFISIOLOGÍA CLÍNICA")
        ) is None

    def test_nutricion_neurodegenerativa_is_not_high_risk(self):
        """Nutrition support, not core neurology."""
        assert _high_risk_specialty_default(
            _spec("NUTRICIÓN PATOLOGÍA NEURODEGENERATIVA")
        ) is None

    def test_ecocardiograma_is_not_high_risk(self):
        """Diagnostic test, not cardiology care."""
        assert _high_risk_specialty_default(
            _spec("ECOCARDIOGRAMA DOPPLER")
        ) is None


class TestHighRiskTruePositives:
    """Sanity: the catalog entries that SHOULD trigger urgent default
    still do after the allow-list refactor."""

    def test_cardiologia_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("CARDIOLOGÍA")) == 1

    def test_cardiologia_infantil_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("CARDIOLOGÍA INFANTIL")) == 1

    def test_cirugia_cardiovascular_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("CIRUGÍA CARDIOVASCULAR")) == 1

    def test_oncologia_medica_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("ONCOLOGÍA MÉDICA")) == 1

    def test_oncologia_radioterapica_is_high_risk(self):
        assert _high_risk_specialty_default(
            _spec("ONCOLOGÍA RADIOTERÁPICA")
        ) == 1

    def test_neurologia_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("NEUROLOGÍA")) == 1

    def test_neurocirugia_is_high_risk(self):
        assert _high_risk_specialty_default(_spec("NEUROCIRUGÍA")) == 1


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


# ---- gender preference (WhatsApp follow-up reply parsing) ----

class TestGenderReplyParsing:
    """Tests for _parse_gender_reply used by the WhatsApp webhook when the
    user responds to the 'do you prefer a male or female doctor?' question."""

    def test_doctora_returns_female(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("doctora") == "female"

    def test_mujer_returns_female(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("mujer") == "female"

    def test_female_returns_female(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("female") == "female"

    def test_doctor_returns_male(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("doctor") == "male"

    def test_hombre_returns_male(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("hombre") == "male"

    def test_male_returns_male(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("male") == "male"

    def test_no_returns_skip(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("no") == "skip"

    def test_sin_preferencia_returns_skip(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("sin preferencia") == "skip"

    def test_cualquiera_returns_skip(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("cualquiera") == "skip"

    def test_unrecognized_defaults_to_skip(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("blah blah") == "skip"

    def test_case_insensitive(self):
        from interfaces.webhooks.whatsapp_webhook import _parse_gender_reply
        assert _parse_gender_reply("DOCTORA") == "female"
        assert _parse_gender_reply("Doctor") == "male"


# ---- gender filtering (doctor list) ----

class TestDoctorGenderFiltering:
    """Tests for _infer_doctor_gender and _filter_doctors_by_gender."""

    def test_infer_female_from_soledad(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("HERMOSO IZQUIERDO, SOLEDAD") == "female"

    def test_infer_female_from_maria(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("GARCÍA LÓPEZ, MARÍA") == "female"

    def test_infer_female_from_iluminada(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("RUBIO, ILUMINADA") == "female"

    def test_infer_female_from_ana_compound(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("ROMERO, ANA ISABEL") == "female"

    def test_infer_male_from_carlos(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("FERNÁNDEZ GÓMEZ, CARLOS") == "male"

    def test_infer_male_from_juan(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("MARTÍN, JUAN ANTONIO") == "male"

    def test_infer_none_for_unknown_name(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("SMITH, ZXYQW") is None

    def test_infer_none_for_no_comma(self):
        from application.book_appointment import _infer_doctor_gender
        assert _infer_doctor_gender("SINGLE NAME") is None

    def test_filter_keeps_only_female(self):
        from application.book_appointment import _filter_doctors_by_gender
        from domain.entities.doctor import Doctor
        from domain.value_objects.address import Address
        from domain.value_objects.specialty import Specialty, SpecialtyType

        sp = Specialty(name="PSICOLOGIA", type=SpecialtyType.SPECIALTY)
        addr = Address(raw="C. Test, MADRID", city="MADRID", postal_code="28001")
        d1 = Doctor(
            clinic_id="1", practitioner_id="1", name="RUBIO, ILUMINADA",
            specialty=sp, clinic_name="C1", address=addr, phone="911111111",
        )
        d2 = Doctor(
            clinic_id="2", practitioner_id="2", name="FERNÁNDEZ, CARLOS",
            specialty=sp, clinic_name="C2", address=addr, phone="922222222",
        )
        d3 = Doctor(
            clinic_id="3", practitioner_id="3", name="GARCÍA, MARÍA",
            specialty=sp, clinic_name="C3", address=addr, phone="933333333",
        )
        result = _filter_doctors_by_gender([d1, d2, d3], "female")
        assert len(result) == 2
        assert all(d.name in ("RUBIO, ILUMINADA", "GARCÍA, MARÍA") for d in result)

    def test_filter_keeps_only_male(self):
        from application.book_appointment import _filter_doctors_by_gender
        from domain.entities.doctor import Doctor
        from domain.value_objects.address import Address
        from domain.value_objects.specialty import Specialty, SpecialtyType

        sp = Specialty(name="PSICOLOGIA", type=SpecialtyType.SPECIALTY)
        addr = Address(raw="C. Test, MADRID", city="MADRID", postal_code="28001")
        d1 = Doctor(
            clinic_id="1", practitioner_id="1", name="RUBIO, ILUMINADA",
            specialty=sp, clinic_name="C1", address=addr, phone="911111111",
        )
        d2 = Doctor(
            clinic_id="2", practitioner_id="2", name="FERNÁNDEZ, CARLOS",
            specialty=sp, clinic_name="C2", address=addr, phone="922222222",
        )
        result = _filter_doctors_by_gender([d1, d2], "male")
        assert len(result) == 1
        assert result[0].name == "FERNÁNDEZ, CARLOS"
