"""Doctor and Clinic entities."""
from __future__ import annotations

from dataclasses import dataclass

from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty


@dataclass(frozen=True)
class Doctor:
    """A doctor offering appointments at a specific clinic.

    One `Doctor` instance represents the (practitioner × clinic) tuple — if
    Dr. Hermoso practices at 3 clinics, that's 3 `Doctor` instances in a
    search result. This matches how booking actually works (you call THIS
    clinic to book THIS doctor at THIS address).

    Identifiers for Cigna:
      - `clinic_id`     → addresses[].id from the advanced-search API
                          (e.g. 1501103525). Stable for "Dr. X AT clinic Y".
      - `practitioner_id` → the top-level Cigna id for the practitioner
                            (e.g. "-1402186762"). Useful for "find all clinics
                            for Dr. X" lookups.
      - `clinic_org_id` → addresses[].idProvider from the API
                          (e.g. "13689120"). The CLINIC organization id —
                          **shared across all practitioners at the same clinic.**
                          The voice caller groups by this field so we don't
                          dial the same clinic switchboard N times when N
                          doctors work there. Optional / may be empty string
                          for entries where Cigna didn't expose it.
    """

    clinic_id: str
    practitioner_id: str
    name: str
    specialty: Specialty
    clinic_name: str
    address: Address
    phone: str
    clinic_org_id: str = ""
    distance_meters: int | None = None
    rating: float | None = None
