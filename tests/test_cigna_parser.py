"""Parser tests — `_parse_doctors_response` takes raw `content[]` from Cigna's
advanced-search API and turns it into Doctor entities.

The fixtures here mirror the real Cigna response shape (verified against
live data, May 2026). Field names and types are what the API actually
returns; if Cigna changes the contract these tests will catch it.
"""
from __future__ import annotations

from domain.entities.doctor import Doctor
from domain.value_objects.address import Coordinates
from domain.value_objects.specialty import get_catalog
from infrastructure.scrapers.cigna_scraper import _parse_doctors_response

# The parser embeds whatever Specialty we pass it on each Doctor — use a real
# entry from the bundled catalog so the value object is exercised genuinely.
SPECIALTY = get_catalog().find_by_name("PSICOLOGIA")
assert SPECIALTY is not None


# ---- Fixtures ----

ONE_DOCTOR_ONE_CLINIC = [
    {
        "id": "-1402186762",
        "name": "HERMOSO IZQUIERDO, SOLEDAD",
        "addresses": [
            {
                "id": 1501103525,
                "idProvider": "13689120",
                "provider": "SH PSICOSALUD",
                "address": "C. Don Pedro, 17",
                "city": "MADRID",
                "phone1": "915631554",
                "geoPoint": {"lat": 40.411784, "lon": -3.714223},
                "postcd": "28005",
                "distanceToSearchPoint": 0.45,
            }
        ],
    }
]

# Two distinct practitioners at the same clinic — same idProvider, same phone,
# but different addresses[].id and different practitioner ids.
TWO_DOCTORS_SAME_CLINIC = [
    {
        "id": "111",
        "name": "RUBIO, ILUMINADA",
        "addresses": [
            {
                "id": 1001,
                "idProvider": "13689120",
                "provider": "CLÍNICA ARMSTRONG INTERNACIONAL",
                "address": "C/ Guzmán El Bueno 102",
                "city": "MADRID",
                "phone1": "915358790",
                "postcd": "28003",
                "distanceToSearchPoint": 0.75,
            }
        ],
    },
    {
        "id": "222",
        "name": "ROMERO, ANA ISABEL",
        "addresses": [
            {
                "id": 1002,
                "idProvider": "13689120",
                "provider": "CLÍNICA ARMSTRONG INTERNACIONAL",
                "address": "C/ Guzmán El Bueno 102",
                "city": "MADRID",
                "phone1": "915358790",
                "postcd": "28003",
                "distanceToSearchPoint": 0.75,
            }
        ],
    },
]

ONE_DOCTOR_THREE_CLINICS = [
    {
        "id": "987654321",
        "name": "GARCÍA LÓPEZ, MARÍA",
        "addresses": [
            {
                "id": 1000,
                "provider": "CLÍNICA A",
                "address": "Calle Uno, 1",
                "city": "MADRID",
                "phone1": "910000001",
                "geoPoint": {"lat": 40.4, "lon": -3.7},
                "postcd": "28001",
                "distanceToSearchPoint": 0.5,
            },
            {
                "id": 2000,
                "provider": "CLÍNICA B",
                "address": "Calle Dos, 2",
                "city": "MADRID",
                "phone1": "910000002",
                "geoPoint": {"lat": 40.41, "lon": -3.71},
                "postcd": "28002",
                "distanceToSearchPoint": 1.2,
            },
            {
                "id": 3000,
                "provider": "CLÍNICA C",
                "address": "Calle Tres, 3",
                "city": "MADRID",
                "phone1": "910000003",
                "geoPoint": {"lat": 40.42, "lon": -3.72},
                "postcd": "28003",
                "distanceToSearchPoint": 2.8,
            },
        ],
    }
]


# ---- Happy path ----

class TestParseHappyPath:
    def test_one_doctor_one_clinic(self):
        result = _parse_doctors_response(ONE_DOCTOR_ONE_CLINIC, SPECIALTY)
        assert len(result) == 1
        d = result[0]
        assert isinstance(d, Doctor)
        assert d.practitioner_id == "-1402186762"
        assert d.clinic_id == "1501103525"
        assert d.clinic_org_id == "13689120"
        assert d.name == "HERMOSO IZQUIERDO, SOLEDAD"
        assert d.clinic_name == "SH PSICOSALUD"
        assert d.specialty.name == "PSICOLOGIA"
        assert d.phone == "915631554"
        assert d.distance_meters == 450  # 0.45 km → 450 m

    def test_doctors_at_same_clinic_share_clinic_org_id(self):
        """`addresses[].idProvider` is the same across all practitioners at
        the same clinic. The voice caller will group by this field."""
        result = _parse_doctors_response(TWO_DOCTORS_SAME_CLINIC, SPECIALTY)
        assert len(result) == 2
        assert result[0].name == "RUBIO, ILUMINADA"
        assert result[1].name == "ROMERO, ANA ISABEL"
        # Same clinic org, same phone — different practitioner ids
        assert result[0].clinic_org_id == result[1].clinic_org_id == "13689120"
        assert result[0].phone == result[1].phone == "915358790"
        assert result[0].practitioner_id != result[1].practitioner_id

    def test_address_carries_coordinates(self):
        result = _parse_doctors_response(ONE_DOCTOR_ONE_CLINIC, SPECIALTY)
        d = result[0]
        assert d.address.raw == "C. Don Pedro, 17"
        assert d.address.city == "MADRID"
        assert d.address.postal_code == "28005"
        assert d.address.country == "ES"
        assert d.address.coordinates == Coordinates(
            latitude=40.411784, longitude=-3.714223
        )

    def test_one_doctor_with_n_clinics_emits_n_doctors(self):
        """Same practitioner at 3 clinics → 3 Doctor instances. Each clinic
        is a separate booking target."""
        result = _parse_doctors_response(ONE_DOCTOR_THREE_CLINICS, SPECIALTY)
        assert len(result) == 3
        assert all(d.practitioner_id == "987654321" for d in result)
        # Each has a distinct clinic_id and distance
        assert {d.clinic_id for d in result} == {"1000", "2000", "3000"}
        assert {d.distance_meters for d in result} == {500, 1200, 2800}

    def test_results_preserve_input_order(self):
        result = _parse_doctors_response(ONE_DOCTOR_THREE_CLINICS, SPECIALTY)
        clinic_ids = [d.clinic_id for d in result]
        assert clinic_ids == ["1000", "2000", "3000"]


# ---- Edge cases ----

class TestParseEdgeCases:
    def test_empty_input_returns_empty_list(self):
        assert _parse_doctors_response([], SPECIALTY) == []

    def test_entry_with_no_addresses_is_skipped(self):
        """No clinic to book at → no Doctor instance."""
        raw = [
            {"id": "1", "name": "DR NO ADDRESSES", "addresses": []},
            {"id": "2", "name": "DR HAS ONE"} | ONE_DOCTOR_ONE_CLINIC[0],
        ]
        # The second entry overwrites `id`, `name`, `addresses` with the
        # original ones from ONE_DOCTOR_ONE_CLINIC[0].
        result = _parse_doctors_response(raw, SPECIALTY)
        assert len(result) == 1
        assert result[0].clinic_id == "1501103525"

    def test_missing_geo_point_returns_none_coordinates(self):
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "9",
                        # geoPoint omitted
                        "distanceToSearchPoint": 1.0,
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.address.coordinates is None

    def test_missing_id_provider_yields_empty_clinic_org_id(self):
        """Some Cigna entries omit `idProvider`; we store empty string and
        log no warning — the voice caller's grouping will fall back to phone."""
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "9",
                        # idProvider omitted
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.clinic_org_id == ""

    def test_partial_geo_point_returns_none_coordinates(self):
        """Half-populated geo points are treated as missing."""
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "9",
                        "geoPoint": {"lat": 40.0},  # missing 'lon'
                        "distanceToSearchPoint": 1.0,
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.address.coordinates is None

    def test_missing_distance_returns_none(self):
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "9",
                        # distanceToSearchPoint omitted
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.distance_meters is None

    def test_falls_back_to_practitioner_name_when_clinic_provider_missing(self):
        raw = [
            {
                "id": "1",
                "name": "DR FALLBACK",
                "addresses": [{"id": 100, "address": "X", "phone1": "9"}],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.clinic_name == "DR FALLBACK"

    def test_phone_is_cleaned(self):
        """Strips non-digit characters except leading +."""
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "(91) 563 15 54",
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.phone == "915631554"

    def test_distance_zero_means_at_search_point(self):
        """0.0 km is a valid distance, not None."""
        raw = [
            {
                "id": "1",
                "name": "DR",
                "addresses": [
                    {
                        "id": 100,
                        "provider": "CLINIC",
                        "address": "X",
                        "phone1": "9",
                        "distanceToSearchPoint": 0.0,
                    }
                ],
            }
        ]
        d = _parse_doctors_response(raw, SPECIALTY)[0]
        assert d.distance_meters == 0
