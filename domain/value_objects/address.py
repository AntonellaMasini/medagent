"""Address value object."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Address:
    """A postal address. Coordinates are optional and filled in by geocoding."""

    raw: str
    city: str | None = None
    postal_code: str | None = None
    country: str = "ES"
    coordinates: Coordinates | None = None

    def with_coordinates(self, coords: Coordinates) -> "Address":
        return Address(
            raw=self.raw,
            city=self.city,
            postal_code=self.postal_code,
            country=self.country,
            coordinates=coords,
        )
