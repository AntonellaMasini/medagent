"""Time-related value objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

DEFAULT_TRAVEL_BUFFER_MINUTES = 45


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
        return self.start + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class AvailabilityWindow:
    """User's preferences on when they're available to be seen."""

    preferred: TimePreference = TimePreference.ANY
    excluded_days: frozenset[Weekday] = field(default_factory=frozenset)
    earliest: time = time(9, 0)
    latest: time = time(20, 0)
    max_weeks_out: int = 4
    travel_buffer_minutes: int = DEFAULT_TRAVEL_BUFFER_MINUTES

    def accepts(
        self,
        slot: TimeSlot,
        last_event_end: datetime | None = None,
    ) -> bool:
        """Whether this slot fits the user's availability preferences.

        `last_event_end`: when the user's last calendar event before `slot`
        ends. Used to enforce the travel buffer. If None, the travel-buffer
        check is skipped (caller hasn't queried the calendar yet).
        """
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

        if last_event_end is not None and last_event_end <= slot.start:
            gap = slot.start - last_event_end
            if gap < timedelta(minutes=self.travel_buffer_minutes):
                return False

        return True
