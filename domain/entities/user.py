"""User entity."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from domain.value_objects.address import Address
from domain.value_objects.time_slot import AvailabilityWindow


class Insurer(str, Enum):
    CIGNA = "cigna"
    ADESLAS = "adeslas"
    SANITAS = "sanitas"


@dataclass
class InsurerCredentials:
    """Login credentials for an insurer portal. Stored encrypted at rest."""

    insurer: Insurer
    username: str  # NIE / NIF / passport
    password: str


@dataclass
class User:
    """A registered user of the agent."""

    phone: str  # E.164 format, e.g. "+34612345678"
    name: str
    home_address: Address
    insurer_credentials: InsurerCredentials
    availability: AvailabilityWindow = field(default_factory=AvailabilityWindow)
    google_calendar_token: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def insurer(self) -> Insurer:
        return self.insurer_credentials.insurer
