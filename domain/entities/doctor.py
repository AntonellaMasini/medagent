"""Doctor and Clinic entities."""
from __future__ import annotations

from dataclasses import dataclass

from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty


@dataclass(frozen=True)
class Doctor:
    """A doctor/clinic listing as returned by an insurer's cuadro médico."""

    id: str  # stable identifier within the insurer (e.g. cigna doctor id)
    name: str
    specialty: Specialty
    clinic_name: str
    address: Address
    phone: str
    distance_meters: int | None = None
    rating: float | None = None
