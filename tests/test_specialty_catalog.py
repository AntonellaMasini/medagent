"""SpecialtyCatalog: free-text search against Cigna's specialty catalog.

These tests pin down the search algorithm's intended behaviour — what
counts as a confident match, what's ambiguous, and how type priority
breaks ties. The Cigna JSON catalog is loaded as fixture data, so these
tests will fail loudly if a future schema/data refresh changes assumptions.
"""
from __future__ import annotations

import pytest

from domain.value_objects.specialty import (
    Specialty,
    SpecialtyCatalog,
    SpecialtyType,
    get_catalog,
    normalize_specialty,
)


# ---- catalog load + invariants ----

class TestCatalogShape:
    def test_loads_expected_total(self):
        assert len(get_catalog().entries) == 194

    def test_names_are_unique(self):
        names = [e.name for e in get_catalog().entries]
        assert len(names) == len(set(names))

    def test_each_entry_has_a_type(self):
        for e in get_catalog().entries:
            assert e.type in (
                SpecialtyType.SPECIALTY,
                SpecialtyType.SUB_SPECIALTY,
                SpecialtyType.MEDICAL_ACT,
            )

    def test_some_catalog_ids_are_empty(self):
        """Document the Cigna quirk: ~32 entries have no catalogId.

        If this assertion ever fails, Cigna has cleaned up their catalog
        and we can revisit whether to make `catalog_id` a real identifier.
        """
        empty = [e for e in get_catalog().entries if not e.catalog_id]
        assert len(empty) > 0
        # PSICOLOGIA is one of them — sanity-check the most-affected entry
        assert any(e.name == "PSICOLOGIA" for e in empty)


# ---- find_by_* ----

class TestDirectLookups:
    def test_find_by_name_exact(self):
        s = get_catalog().find_by_name("DERMATOLOGÍA")
        assert s is not None
        assert s.catalog_id == "E016"  # may be brittle if Cigna renumbers
        assert s.type == SpecialtyType.SPECIALTY

    def test_find_by_name_accent_insensitive(self):
        assert get_catalog().find_by_name("DERMATOLOGIA") is not None
        assert get_catalog().find_by_name("dermatologia") is not None

    def test_find_by_name_unknown(self):
        assert get_catalog().find_by_name("not a real specialty") is None

    def test_find_by_catalog_id_works_when_present(self):
        s = get_catalog().find_by_catalog_id("E016")
        assert s is not None and s.name == "DERMATOLOGÍA"

    def test_find_by_catalog_id_rejects_empty(self):
        # Empty catalog_id wouldn't disambiguate (multiple entries have ""), so
        # we explicitly refuse it.
        assert get_catalog().find_by_catalog_id("") is None


# ---- search: happy path ----

class TestSearch:
    @pytest.mark.parametrize(
        "query, expected",
        [
            # Exact / accent-insensitive
            ("PSICOLOGIA", "PSICOLOGIA"),
            ("psicologia", "PSICOLOGIA"),
            ("DERMATOLOGÍA", "DERMATOLOGÍA"),
            ("dermatologia", "DERMATOLOGÍA"),
            # Gender variants (-o / -a) on -logía specialties
            ("psicologo", "PSICOLOGIA"),
            ("psicóloga", "PSICOLOGIA"),
            ("dermatólogo", "DERMATOLOGÍA"),
            ("dermatóloga", "DERMATOLOGÍA"),
            ("cardiologo", "CARDIOLOGÍA"),
            ("cardiologa", "CARDIOLOGÍA"),
            ("urologo", "UROLOGÍA"),
            ("alergologo", "ALERGOLOGÍA"),
            ("neurologo", "NEUROLOGÍA"),
            # Short forms anchored at the name's start
            ("cardio", "CARDIOLOGÍA"),
            ("fisio", "FISIOTERAPIA"),
            ("trauma", "TRAUMATOLOGÍA Y CIRUGÍA ORTOPÉDICA"),
            # Head-word match wins over a non-first-word match
            ("traumatologo", "TRAUMATOLOGÍA Y CIRUGÍA ORTOPÉDICA"),
            # Non-first-word gender-tolerant match
            ("ginecologo", "OBSTETRICIA Y GINECOLOGÍA"),
            ("ginecologa", "OBSTETRICIA Y GINECOLOGÍA"),
            # Multi-word query
            ("cardiologia infantil", "CARDIOLOGÍA INFANTIL"),
            # Special: catalog has "PSIQUIATRÍA" with accent
            ("psiquiatra", "PSIQUIATRÍA"),
        ],
    )
    def test_matches(self, query, expected):
        result = normalize_specialty(query)
        assert result is not None, f"{query!r} should match {expected!r}"
        assert result.name == expected


# ---- search: synonyms ----

class TestSynonyms:
    @pytest.mark.parametrize(
        "query, expected",
        [
            ("dentista", "ODONTOLOGIA"),
            ("oculista", "OFTALMOLOGÍA"),
            ("ojos", "OFTALMOLOGÍA"),
            ("internista", "MEDICINA INTERNA"),
            ("orl", "OTORRINOLARINGOLOGÍA"),
            ("garganta", "OTORRINOLARINGOLOGÍA"),
            # -terapeuta / -ología (suffix delta > 2 chars → needs synonym)
            ("fisioterapeuta", "FISIOTERAPIA"),
            ("kinesiologo", "FISIOTERAPIA"),
        ],
    )
    def test_synonym_resolves(self, query, expected):
        result = normalize_specialty(query)
        assert result is not None
        assert result.name == expected


# ---- search: type priority ----

class TestTypePriority:
    def test_specialty_wins_over_medical_act_for_same_name(self):
        """`OBSTETRICIA` exists as both a MEDICAL_ACT and inside the SPECIALTY
        name `OBSTETRICIA Y GINECOLOGÍA`. The SPECIALTY should win."""
        result = normalize_specialty("obstetricia")
        assert result is not None
        assert result.name == "OBSTETRICIA Y GINECOLOGÍA"
        assert result.type == SpecialtyType.SPECIALTY

    def test_specialty_wins_over_sub_specialty(self):
        """`cardio` matches both CARDIOLOGÍA (SPECIALTY) and several
        CARDIOLOGÍA-prefixed SUB_SPECIALTYs. SPECIALTY wins."""
        result = normalize_specialty("cardio")
        assert result is not None
        assert result.type == SpecialtyType.SPECIALTY


# ---- search: ambiguity / no-match ----

class TestNoConfidentMatch:
    @pytest.mark.parametrize(
        "query",
        [
            # Modifier words that appear in many entries — no anchor
            "infantil",
            "general",  # MEDICINA GENERAL vs CIRUGÍA GENERAL Y DEL APARATO DIGESTIVO etc
            # Generic non-specialty terms
            "doctor",
            "médico",
            # Symptoms (LLM territory, deferred to issue #15)
            "me duele la cabeza",
            "creo que me he torcido el tobillo",
            # Garbage / out-of-language
            "xyz",
            "sigmund",
            "totally bogus query string",
            # Short noise
            "un",
            "el",
            "la",
            # Empty / whitespace
            "",
            "   ",
        ],
    )
    def test_returns_none(self, query):
        assert normalize_specialty(query) is None


# ---- catalog construction from explicit entries (for unit testing search) ----

class TestSearchWithFixtureCatalog:
    """Verify the search algorithm with a hand-built tiny catalog so the
    invariants don't depend on Cigna's data staying fixed."""

    @pytest.fixture
    def tiny(self) -> SpecialtyCatalog:
        return SpecialtyCatalog(entries=(
            Specialty("CARDIOLOGÍA", SpecialtyType.SPECIALTY, "X001"),
            Specialty("CARDIOLOGÍA INFANTIL", SpecialtyType.SUB_SPECIALTY, "X002"),
            Specialty("CIRUGÍA CARDIOVASCULAR", SpecialtyType.SPECIALTY, "X003"),
            Specialty("DERMATOLOGÍA", SpecialtyType.SPECIALTY, "X004"),
        ))

    def test_exact(self, tiny):
        assert tiny.search("CARDIOLOGÍA").name == "CARDIOLOGÍA"

    def test_head_word_anchor_beats_modifier_anchor(self, tiny):
        # "cardio" matches CARDIOLOGÍA (head-word startswith) and
        # CIRUGÍA CARDIOVASCULAR (non-first word startswith). Head wins.
        assert tiny.search("cardio").name == "CARDIOLOGÍA"

    def test_specialty_beats_sub_specialty(self, tiny):
        # Both CARDIOLOGÍA and CARDIOLOGÍA INFANTIL are head-word startswith
        # matches for "cardio". SPECIALTY wins.
        assert tiny.search("cardio").type == SpecialtyType.SPECIALTY

    def test_returns_none_on_empty_catalog(self):
        assert SpecialtyCatalog(entries=()).search("anything") is None
