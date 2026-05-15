"""Time-related value objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum


class TimePreference(str, Enum):
    MORNINGS = "mornings"
    AFTERNOONS = "afternoons"
    ANY = "any"


class Weekday(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


@dataclass(frozen=True)
class TimeSlot:
    """A concrete moment in time when an appointment is offered/booked."""

    start: datetime
    duration_minutes: int = 30

    @property
    def end(self) -> datetime:
        from datetime import timedelta

        return self.start + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class AvailabilityWindow:
    """User's preferences on when they're available to be seen."""

    preferred: TimePreference = TimePreference.ANY
    excluded_days: frozenset[Weekday] = field(default_factory=frozenset)
    earliest: time = time(9, 0)
    latest: time = time(20, 0)
    max_weeks_out: int = 4

    def accepts(self, slot: TimeSlot) -> bool:
        """Whether this slot fits the user's availability preferences."""
        weekday = Weekday(slot.start.strftime("%A").lower())
        if weekday in self.excluded_days:
            return False
        slot_time = slot.start.time()
        if slot_time < self.earliest or slot_time > self.latest:
            return False
        if self.preferred == TimePreference.MORNINGS and slot_time >= time(14, 0):
            return False
        if self.preferred == TimePreference.AFTERNOONS and slot_time < time(14, 0):
            return False
        return True
