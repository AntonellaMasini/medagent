"""Slot validation: travel buffer + blocked windows."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from domain.value_objects.time_slot import (
    DEFAULT_TRAVEL_BUFFER_MINUTES,
    AvailabilityWindow,
    BlockedWindow,
    TimeSlot,
    Weekday,
)


# Monday 2026-05-18, Wednesday 2026-05-20 — fixed weekdays for the tests below.
MONDAY = datetime(2026, 5, 18)
WEDNESDAY = datetime(2026, 5, 20)


def _slot(dt: datetime, minutes: int = 30) -> TimeSlot:
    return TimeSlot(start=dt, duration_minutes=minutes)


# ---- BlockedWindow.overlaps ----

class TestBlockedWindow:
    def test_overlaps_when_slot_starts_inside_window(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        assert bw.overlaps(_slot(MONDAY.replace(hour=16, minute=0)))

    def test_overlaps_when_slot_ends_inside_window(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        # 15:00–15:45 overlaps a 15:30 start
        assert bw.overlaps(_slot(MONDAY.replace(hour=15, minute=0), minutes=45))

    def test_does_not_overlap_when_slot_ends_at_window_start(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        # 15:00–15:30 ends exactly when the window starts — half-open boundary
        assert not bw.overlaps(_slot(MONDAY.replace(hour=15, minute=0), minutes=30))

    def test_does_not_overlap_when_slot_starts_at_window_end(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        assert not bw.overlaps(_slot(MONDAY.replace(hour=17, minute=0)))

    def test_does_not_overlap_on_other_weekday(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        # Wednesday 16:00 — same time, different day
        assert not bw.overlaps(_slot(WEDNESDAY.replace(hour=16, minute=0)))

    def test_overlaps_on_any_listed_day(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY, Weekday.WEDNESDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        assert bw.overlaps(_slot(MONDAY.replace(hour=16, minute=0)))
        assert bw.overlaps(_slot(WEDNESDAY.replace(hour=16, minute=0)))

    def test_rejects_invalid_construction(self):
        with pytest.raises(ValueError):
            BlockedWindow(
                days=frozenset({Weekday.MONDAY}),
                from_time=time(17, 0),
                to_time=time(15, 30),
            )
        with pytest.raises(ValueError):
            BlockedWindow(days=frozenset(), from_time=time(9, 0), to_time=time(10, 0))


# ---- AvailabilityWindow.accepts with travel buffer ----

class TestTravelBuffer:
    def test_default_buffer_is_45_minutes(self):
        aw = AvailabilityWindow()
        assert aw.travel_buffer_minutes == DEFAULT_TRAVEL_BUFFER_MINUTES == 45

    def test_skips_check_when_last_event_end_is_none(self):
        aw = AvailabilityWindow(travel_buffer_minutes=45)
        assert aw.accepts(_slot(MONDAY.replace(hour=10)), last_event_end=None)

    def test_rejects_when_gap_below_buffer(self):
        aw = AvailabilityWindow(travel_buffer_minutes=45)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        # Previous event ends at 9:30 → 30 min gap < 45 min buffer
        last = MONDAY.replace(hour=9, minute=30)
        assert not aw.accepts(slot, last_event_end=last)

    def test_accepts_when_gap_meets_buffer_exactly(self):
        aw = AvailabilityWindow(travel_buffer_minutes=45)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        last = MONDAY.replace(hour=9, minute=15)  # exactly 45 min before
        assert aw.accepts(slot, last_event_end=last)

    def test_accepts_when_gap_above_buffer(self):
        aw = AvailabilityWindow(travel_buffer_minutes=45)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        last = MONDAY.replace(hour=8, minute=0)  # 2 hours before
        assert aw.accepts(slot, last_event_end=last)

    def test_ignores_events_after_the_slot(self):
        aw = AvailabilityWindow(travel_buffer_minutes=45)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        # A "last event" that ends *after* the slot is from a later slot —
        # caller passed the wrong thing. Behave safely: don't reject the slot.
        future_event_end = MONDAY.replace(hour=12, minute=0)
        assert aw.accepts(slot, last_event_end=future_event_end)

    def test_zero_buffer_allows_back_to_back(self):
        aw = AvailabilityWindow(travel_buffer_minutes=0)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        last = MONDAY.replace(hour=10, minute=0)
        assert aw.accepts(slot, last_event_end=last)

    def test_custom_buffer_applied(self):
        aw = AvailabilityWindow(travel_buffer_minutes=90)
        slot = _slot(MONDAY.replace(hour=10, minute=0))
        # 60 min gap with a 90 min buffer → reject
        assert not aw.accepts(
            slot, last_event_end=MONDAY.replace(hour=9, minute=0)
        )


# ---- AvailabilityWindow.accepts with blocked windows ----

class TestBlockedWindowsInAccepts:
    def test_rejects_slot_overlapping_blocked_window(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        aw = AvailabilityWindow(blocked_windows=(bw,))
        assert not aw.accepts(_slot(MONDAY.replace(hour=16)))

    def test_accepts_slot_outside_blocked_window(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        aw = AvailabilityWindow(blocked_windows=(bw,))
        # Same day, different time
        assert aw.accepts(_slot(MONDAY.replace(hour=11)))

    def test_multiple_blocked_windows_any_blocks(self):
        a = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(12, 0),
            to_time=time(13, 0),
        )
        b = BlockedWindow(
            days=frozenset({Weekday.WEDNESDAY}),
            from_time=time(16, 0),
            to_time=time(18, 0),
        )
        aw = AvailabilityWindow(blocked_windows=(a, b))
        assert not aw.accepts(_slot(MONDAY.replace(hour=12, minute=30)))
        assert not aw.accepts(_slot(WEDNESDAY.replace(hour=17)))
        assert aw.accepts(_slot(MONDAY.replace(hour=14)))

    def test_blocked_windows_combined_with_travel_buffer(self):
        bw = BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )
        aw = AvailabilityWindow(travel_buffer_minutes=45, blocked_windows=(bw,))
        # Slot at 11:00 on monday with last event at 10:55 → buffer fails
        slot = _slot(MONDAY.replace(hour=11))
        assert not aw.accepts(
            slot, last_event_end=MONDAY.replace(hour=10, minute=55)
        )
        # Same slot, no prior event → fine (not in blocked window)
        assert aw.accepts(slot)
        # 16:00 slot, no prior event → still rejected (blocked)
        assert not aw.accepts(_slot(MONDAY.replace(hour=16)))


# ---- Existing accepts() behavior still holds ----

class TestExistingAcceptsBehavior:
    def test_excluded_day_still_rejects(self):
        aw = AvailabilityWindow(excluded_days=frozenset({Weekday.MONDAY}))
        assert not aw.accepts(_slot(MONDAY.replace(hour=10)))

    def test_outside_hours_rejected(self):
        aw = AvailabilityWindow(earliest=time(9, 0), latest=time(20, 0))
        assert not aw.accepts(_slot(MONDAY.replace(hour=8)))
        assert not aw.accepts(_slot(MONDAY.replace(hour=21)))
        assert aw.accepts(_slot(MONDAY.replace(hour=14)))


# ---- Smoke check on AvailabilityWindow immutability ----

def test_availability_window_is_frozen():
    aw = AvailabilityWindow(travel_buffer_minutes=60)
    with pytest.raises(Exception):
        aw.travel_buffer_minutes = 30  # type: ignore[misc]


def test_buffer_uses_timedelta_arithmetic():
    """Sanity check: buffer compared in real time units, not minutes-as-ints."""
    aw = AvailabilityWindow(travel_buffer_minutes=60)
    slot = _slot(MONDAY.replace(hour=10))
    # 59 minutes 59 seconds → reject
    last = slot.start - timedelta(minutes=59, seconds=59)
    assert not aw.accepts(slot, last_event_end=last)
