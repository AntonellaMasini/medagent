"""Unit tests for the free-text parsers used across onboarding steps."""
from __future__ import annotations

from datetime import time

from application.parsers import (
    is_done,
    is_skip,
    parse_blocked_window,
    parse_excluded_days,
    parse_insurer,
    parse_preferred_time,
    parse_travel_buffer,
)
from domain.value_objects.time_slot import (
    DEFAULT_TRAVEL_BUFFER_MINUTES,
    BlockedWindow,
    TimePreference,
    Weekday,
)


class TestParsePreferredTime:
    def test_english(self):
        assert parse_preferred_time("mornings") == TimePreference.MORNINGS
        assert parse_preferred_time("morning") == TimePreference.MORNINGS
        assert parse_preferred_time("afternoons") == TimePreference.AFTERNOONS
        assert parse_preferred_time("any") == TimePreference.ANY
        assert parse_preferred_time("anytime") == TimePreference.ANY

    def test_spanish(self):
        assert parse_preferred_time("mañanas") == TimePreference.MORNINGS
        assert parse_preferred_time("tardes") == TimePreference.AFTERNOONS
        assert parse_preferred_time("cualquiera") == TimePreference.ANY

    def test_case_insensitive(self):
        assert parse_preferred_time("MORNINGS") == TimePreference.MORNINGS
        assert parse_preferred_time(" Any ") == TimePreference.ANY

    def test_returns_none_for_garbage(self):
        assert parse_preferred_time("") is None
        assert parse_preferred_time("whenever I feel like it") is None


class TestParseInsurer:
    def test_cigna_normalized(self):
        assert parse_insurer("Cigna") == "cigna"
        assert parse_insurer("CIGNA") == "cigna"
        assert parse_insurer("I'm with Cigna") == "cigna"

    def test_unsupported_returns_none(self):
        assert parse_insurer("adeslas") is None
        assert parse_insurer("sanitas") is None
        assert parse_insurer("") is None


class TestParseTravelBuffer:
    def test_plain_number_minutes(self):
        assert parse_travel_buffer("45") == 45
        assert parse_travel_buffer("30") == 30

    def test_minutes_with_unit(self):
        assert parse_travel_buffer("60 min") == 60
        assert parse_travel_buffer("90 minutes") == 90

    def test_hours_converted(self):
        assert parse_travel_buffer("1h") == 60
        assert parse_travel_buffer("2 hours") == 120

    def test_skip_returns_default(self):
        assert parse_travel_buffer("skip") == DEFAULT_TRAVEL_BUFFER_MINUTES
        assert parse_travel_buffer("default") == DEFAULT_TRAVEL_BUFFER_MINUTES

    def test_garbage_returns_none(self):
        assert parse_travel_buffer("") is None
        assert parse_travel_buffer("hello") is None

    def test_unreasonable_value(self):
        assert parse_travel_buffer("600 min") is None  # > 8h


class TestParseBlockedWindow:
    def test_single_day(self):
        bw = parse_blocked_window("monday 15:30-17:00")
        assert bw == BlockedWindow(
            days=frozenset({Weekday.MONDAY}),
            from_time=time(15, 30),
            to_time=time(17, 0),
        )

    def test_multi_day(self):
        bw = parse_blocked_window("monday,wednesday 15:30-17:00")
        assert bw is not None
        assert bw.days == frozenset({Weekday.MONDAY, Weekday.WEDNESDAY})

    def test_spanish(self):
        bw = parse_blocked_window("lunes,miércoles 15:30-17:00")
        assert bw is not None
        assert bw.days == frozenset({Weekday.MONDAY, Weekday.WEDNESDAY})

    def test_rejects_inverted(self):
        assert parse_blocked_window("monday 17:00-15:30") is None

    def test_rejects_garbage(self):
        assert parse_blocked_window("garbage") is None
        assert parse_blocked_window("notaday 15:30-17:00") is None


class TestParseExcludedDays:
    def test_basic(self):
        assert parse_excluded_days("saturday,sunday") == frozenset(
            {Weekday.SATURDAY, Weekday.SUNDAY}
        )

    def test_none_returns_empty(self):
        assert parse_excluded_days("none") == frozenset()
        assert parse_excluded_days("skip") == frozenset()

    def test_invalid_day_returns_none(self):
        assert parse_excluded_days("saturday,notaday") is None


def test_is_done_and_is_skip():
    assert is_done("done")
    assert is_done(" LISTO ")
    assert not is_done("monday 15:30-17:00")
    assert is_skip("skip")
    assert is_skip("NONE")
    assert not is_skip("45 min")
