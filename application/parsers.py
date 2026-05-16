"""Free-text parsers reused across onboarding steps.

Kept separate from the state machine so they're easy to unit test and reuse
when future issues add more onboarding questions (e.g. travel buffer,
blocked windows — fields already on AvailabilityWindow but not collected
in the current onboarding flow).
"""
from __future__ import annotations

import re
from datetime import time

from domain.value_objects.time_slot import (
    DEFAULT_TRAVEL_BUFFER_MINUTES,
    BlockedWindow,
    TimePreference,
    Weekday,
)

SKIP_TOKENS = frozenset({"skip", "none", "default", "no", "ninguno", "ninguna", "n/a"})
DONE_TOKENS = frozenset({"done", "ya", "listo", "finish"})

_TRAVEL_BUFFER_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?",
    re.IGNORECASE,
)

WEEKDAY_ALIASES: dict[str, Weekday] = {
    "monday": Weekday.MONDAY, "mon": Weekday.MONDAY, "lunes": Weekday.MONDAY, "l": Weekday.MONDAY,
    "tuesday": Weekday.TUESDAY, "tue": Weekday.TUESDAY, "tues": Weekday.TUESDAY, "martes": Weekday.TUESDAY,
    "wednesday": Weekday.WEDNESDAY, "wed": Weekday.WEDNESDAY, "miercoles": Weekday.WEDNESDAY,
    "thursday": Weekday.THURSDAY, "thu": Weekday.THURSDAY, "thur": Weekday.THURSDAY,
    "thurs": Weekday.THURSDAY, "jueves": Weekday.THURSDAY,
    "friday": Weekday.FRIDAY, "fri": Weekday.FRIDAY, "viernes": Weekday.FRIDAY,
    "saturday": Weekday.SATURDAY, "sat": Weekday.SATURDAY, "sabado": Weekday.SATURDAY,
    "sunday": Weekday.SUNDAY, "sun": Weekday.SUNDAY, "domingo": Weekday.SUNDAY,
}

_BLOCKED_WINDOW_RE = re.compile(
    r"^\s*(?P<days>[a-zA-ZáéíóúÁÉÍÓÚñÑ,\s]+?)\s+"
    r"(?P<from_h>\d{1,2}):(?P<from_m>\d{2})\s*"
    r"(?:-|to|a)\s*"
    r"(?P<to_h>\d{1,2}):(?P<to_m>\d{2})\s*$",
    re.IGNORECASE,
)

ACCENT_MAP = str.maketrans("áéíóúÁÉÍÓÚñÑ", "aeiouAEIOUnN")

_PREFERRED_TIME_ALIASES = {
    "mornings": TimePreference.MORNINGS, "morning": TimePreference.MORNINGS,
    "mañanas": TimePreference.MORNINGS, "manana": TimePreference.MORNINGS,
    "mañana": TimePreference.MORNINGS, "am": TimePreference.MORNINGS,
    "afternoons": TimePreference.AFTERNOONS, "afternoon": TimePreference.AFTERNOONS,
    "tardes": TimePreference.AFTERNOONS, "tarde": TimePreference.AFTERNOONS,
    "pm": TimePreference.AFTERNOONS,
    "any": TimePreference.ANY, "anytime": TimePreference.ANY,
    "cualquiera": TimePreference.ANY, "either": TimePreference.ANY,
    "whenever": TimePreference.ANY, "no preference": TimePreference.ANY,
}


def parse_preferred_time(text: str) -> TimePreference | None:
    if not text:
        return None
    return _PREFERRED_TIME_ALIASES.get(text.strip().lower())


def parse_insurer(text: str) -> str | None:
    """Currently only Cigna is supported.

    Returns 'cigna' if the user typed anything that looks like Cigna,
    otherwise None.
    """
    if not text:
        return None
    cleaned = text.strip().lower().translate(ACCENT_MAP)
    if "cigna" in cleaned:
        return "cigna"
    return None


def parse_travel_buffer(text: str) -> int | None:
    """Minutes of buffer, or None if unparseable.

    'skip'/'default' returns DEFAULT_TRAVEL_BUFFER_MINUTES. Hours convert to
    minutes. Values over 8h are rejected.
    """
    if not text:
        return None
    cleaned = text.strip().lower()
    if cleaned in SKIP_TOKENS:
        return DEFAULT_TRAVEL_BUFFER_MINUTES
    match = _TRAVEL_BUFFER_RE.search(cleaned)
    if not match:
        return None
    value = float(match.group("value"))
    unit = (match.group("unit") or "min").lower()
    minutes = int(value * 60) if unit.startswith("h") else int(value)
    if minutes < 0 or minutes > 8 * 60:
        return None
    return minutes


def parse_blocked_window(text: str) -> BlockedWindow | None:
    """Parse 'monday,wednesday 15:30-17:00' style strings.

    Days accept English or Spanish.
    """
    if not text:
        return None
    match = _BLOCKED_WINDOW_RE.match(text.strip())
    if not match:
        return None
    days_blob = match.group("days").lower().translate(ACCENT_MAP)
    tokens = [t.strip() for t in re.split(r"[,\s]+", days_blob) if t.strip()]
    days: list[Weekday] = []
    for token in tokens:
        if token in WEEKDAY_ALIASES:
            days.append(WEEKDAY_ALIASES[token])
        else:
            return None
    if not days:
        return None
    try:
        from_t = time(int(match.group("from_h")), int(match.group("from_m")))
        to_t = time(int(match.group("to_h")), int(match.group("to_m")))
    except ValueError:
        return None
    if from_t >= to_t:
        return None
    try:
        return BlockedWindow(days=frozenset(days), from_time=from_t, to_time=to_t)
    except ValueError:
        return None


def parse_excluded_days(text: str) -> frozenset[Weekday] | None:
    if not text:
        return None
    cleaned = text.strip().lower().translate(ACCENT_MAP)
    if cleaned in SKIP_TOKENS:
        return frozenset()
    tokens = [t.strip() for t in re.split(r"[,\s]+", cleaned) if t.strip()]
    days: list[Weekday] = []
    for token in tokens:
        if token in WEEKDAY_ALIASES:
            days.append(WEEKDAY_ALIASES[token])
        else:
            return None
    return frozenset(days)


def is_done(text: str) -> bool:
    return (text or "").strip().lower() in DONE_TOKENS


def is_skip(text: str) -> bool:
    return (text or "").strip().lower() in SKIP_TOKENS
