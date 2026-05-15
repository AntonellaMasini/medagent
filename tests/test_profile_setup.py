"""Onboarding parsing helpers + ProfileDraft → User wiring."""
from __future__ import annotations

from datetime import time

from application.setup_user_profile import (
    ProfileDraft,
    is_done,
    is_skip,
    parse_blocked_window,
    parse_travel_buffer,
)
from domain.entities.user import Insurer
from domain.value_objects.time_slot import (
    DEFAULT_TRAVEL_BUFFER_MINUTES,
    BlockedWindow,
    Weekday,
)


class TestParseTravelBuffer:
    def test_plain_number_minutes(self):
        assert parse_travel_buffer("45") == 45
        assert parse_travel_buffer("30") == 30

    def test_minutes_with_unit(self):
        assert parse_travel_buffer("60 min") == 60
        assert parse_travel_buffer("90 minutes") == 90

    def test_hours_converted_to_minutes(self):
        assert parse_travel_buffer("1h") == 60
        assert parse_travel_buffer("1 hour") == 60
        assert parse_travel_buffer("2 hours") == 120

    def test_skip_returns_default(self):
        assert parse_travel_buffer("skip") == DEFAULT_TRAVEL_BUFFER_MINUTES
        assert parse_travel_buffer("default") == DEFAULT_TRAVEL_BUFFER_MINUTES

    def test_empty_or_garbage_is_none(self):
        assert parse_travel_buffer("") is None
        assert parse_travel_buffer("hello") is None

    def test_unreasonable_value_rejected(self):
        # > 8 hours is almost certainly wrong, force a re-ask.
        assert parse_travel_buffer("600 min") is None


class TestParseBlockedWindow:
    def test_basic_single_day(self):
        bw = parse_blocked_window("monday 15:30-17:00")
        assert bw == BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )

    def test_multiple_days_comma_separated(self):
        bw = parse_blocked_window("monday,wednesday 15:30-17:00")
        assert bw is not None
        assert bw.days == frozenset({Weekday.MONDAY, Weekday.WEDNESDAY})

    def test_spanish_days(self):
        bw = parse_blocked_window("lunes,miércoles 15:30-17:00")
        assert bw is not None
        assert bw.days == frozenset({Weekday.MONDAY, Weekday.WEDNESDAY})

    def test_short_day_names(self):
        bw = parse_blocked_window("mon,wed 09:00-10:00")
        assert bw is not None
        assert bw.days == frozenset({Weekday.MONDAY, Weekday.WEDNESDAY})

    def test_to_keyword(self):
        bw = parse_blocked_window("monday 15:30 to 17:00")
        assert bw is not None

    def test_returns_none_when_unparseable(self):
        assert parse_blocked_window("garbage") is None
        assert parse_blocked_window("monday 15:30") is None
        assert parse_blocked_window("notaday 15:30-17:00") is None

    def test_rejects_inverted_times(self):
        assert parse_blocked_window("monday 17:00-15:30") is None


def test_is_done_and_is_skip():
    assert is_done("done")
    assert is_done("DONE")
    assert is_done(" listo ")
    assert not is_done("monday 15:30-17:00")

    assert is_skip("skip")
    assert is_skip("none")
    assert is_skip("DEFAULT")
    assert not is_skip("45 min")


def test_profile_draft_includes_new_fields_in_user():
    draft = ProfileDraft(
        phone="+34612345678",
        name="Test User",
        home_address="Calle Test 1, Madrid",
        insurer=Insurer.CIGNA,
        insurer_username="X12345A",
        insurer_password="pw",
        travel_buffer_minutes=60,
        blocked_windows=[
            BlockedWindow(
                days=frozenset({Weekday.MONDAY}),
                from_time=time(15, 30),
                to_time=time(17, 0),
            )
        ],
    )
    user = draft.to_user()
    assert user.availability.travel_buffer_minutes == 60
    assert len(user.availability.blocked_windows) == 1
    assert user.availability.blocked_windows[0].from_time == time(15, 30)


def test_profile_draft_defaults_match_spec():
    draft = ProfileDraft(phone="+34612345678")
    assert draft.travel_buffer_minutes == DEFAULT_TRAVEL_BUFFER_MINUTES
    assert draft.blocked_windows == []
