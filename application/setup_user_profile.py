"""Use case: collect a new user's profile via WhatsApp."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time

from application.ports import BaseMessagingClient
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.address import Address
from domain.value_objects.time_slot import (
    DEFAULT_TRAVEL_BUFFER_MINUTES,
    AvailabilityWindow,
    BlockedWindow,
    Weekday,
)


@dataclass
class ProfileDraft:
    """In-progress user profile collected step-by-step over WhatsApp."""

    phone: str
    name: str | None = None
    home_address: str | None = None
    insurer: Insurer | None = None
    insurer_username: str | None = None
    insurer_password: str | None = None
    travel_buffer_minutes: int = DEFAULT_TRAVEL_BUFFER_MINUTES
    blocked_windows: list[BlockedWindow] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return all(
            (
                self.name,
                self.home_address,
                self.insurer,
                self.insurer_username,
                self.insurer_password,
            )
        )

    def to_user(self) -> User:
        if not self.is_complete:
            raise ValueError("draft incomplete")
        availability = AvailabilityWindow(
            travel_buffer_minutes=self.travel_buffer_minutes,
            blocked_windows=tuple(self.blocked_windows),
        )
        return User(
            phone=self.phone,
            name=self.name,  # type: ignore[arg-type]
            home_address=Address(raw=self.home_address),  # type: ignore[arg-type]
            insurer_credentials=InsurerCredentials(
                insurer=self.insurer,  # type: ignore[arg-type]
                username=self.insurer_username,  # type: ignore[arg-type]
                password=self.insurer_password,  # type: ignore[arg-type]
            ),
            availability=availability,
        )


class SetupUserProfileUseCase:
    def __init__(self, user_repo: UserRepository, whatsapp: BaseMessagingClient):
        self._user_repo = user_repo
        self._whatsapp = whatsapp

    async def start(self, phone: str) -> None:
        """Kick off the profile-collection flow."""
        await self._whatsapp.send_text(
            phone,
            "Hi! I'll help you book private health appointments. I'll ask you a few "
            "things: your name, address, insurer login, and your calendar "
            "preferences (travel buffer + recurring busy times).\n\n"
            "First — what's your full name?",
        )

    # ---- per-step prompts ----

    async def ask_travel_buffer(self, phone: str) -> None:
        await self._whatsapp.send_text(
            phone,
            "How much travel buffer do you want before an appointment? I'll never "
            f"book one that starts sooner than that after your last calendar event.\n"
            f"Reply with minutes (e.g. '45') or 'skip' for the default "
            f"({DEFAULT_TRAVEL_BUFFER_MINUTES} min).",
        )

    async def ask_blocked_windows(self, phone: str) -> None:
        await self._whatsapp.send_text(
            phone,
            "Any recurring weekly times you never want booked (even when your "
            "calendar shows free)?\nReply with one per message in the form: "
            "'monday,wednesday 15:30-17:00'. Send 'done' when finished, or "
            "'skip' to add none.",
        )

    # ---- response parsers (apply user reply to the draft) ----

    def apply_travel_buffer(self, draft: ProfileDraft, message_body: str) -> bool:
        """Parse a free-text reply and update draft.travel_buffer_minutes.

        Returns True if the reply was understood, False if the caller should
        re-ask. 'skip'/'default' keeps the default.
        """
        minutes = parse_travel_buffer(message_body)
        if minutes is None:
            return False
        draft.travel_buffer_minutes = minutes
        return True

    def apply_blocked_window(self, draft: ProfileDraft, message_body: str) -> bool:
        window = parse_blocked_window(message_body)
        if window is None:
            return False
        draft.blocked_windows.append(window)
        return True

    async def save(self, draft: ProfileDraft) -> User:
        user = draft.to_user()
        await self._user_repo.save(user)
        await self._whatsapp.send_text(
            user.phone,
            f"Thanks {user.name.split()[0]}! You're all set. "
            "Send me a message like 'book me a psychologist' to get started.",
        )
        return user


# ---- free-text parsing helpers ----

_SKIP_TOKENS = {"skip", "none", "default", "no", "ninguno", "ninguna", "n/a"}
_DONE_TOKENS = {"done", "ya", "listo", "finish"}

_TRAVEL_BUFFER_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?",
    re.IGNORECASE,
)

_WEEKDAY_ALIASES: dict[str, Weekday] = {
    "monday": Weekday.MONDAY, "mon": Weekday.MONDAY, "lunes": Weekday.MONDAY, "l": Weekday.MONDAY,
    "tuesday": Weekday.TUESDAY, "tue": Weekday.TUESDAY, "tues": Weekday.TUESDAY, "martes": Weekday.TUESDAY,
    "wednesday": Weekday.WEDNESDAY, "wed": Weekday.WEDNESDAY, "miercoles": Weekday.WEDNESDAY,
    "thursday": Weekday.THURSDAY, "thu": Weekday.THURSDAY, "thur": Weekday.THURSDAY, "thurs": Weekday.THURSDAY, "jueves": Weekday.THURSDAY,
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

_ACCENT_MAP = str.maketrans("áéíóúÁÉÍÓÚñÑ", "aeiouAEIOUnN")


def parse_travel_buffer(text: str) -> int | None:
    """Return travel buffer in minutes, or None if unparseable.

    'skip'/'default' → DEFAULT_TRAVEL_BUFFER_MINUTES.
    Numbers default to minutes. '1h'/'1 hour' multiplied by 60.
    """
    if not text:
        return None
    cleaned = text.strip().lower()
    if cleaned in _SKIP_TOKENS:
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

    Days accept English or Spanish (e.g. 'lunes,miércoles 15:30-17:00').
    Returns None if unparseable; callers should re-prompt.
    """
    if not text:
        return None
    match = _BLOCKED_WINDOW_RE.match(text.strip())
    if not match:
        return None
    days_blob = match.group("days").lower().translate(_ACCENT_MAP)
    day_tokens = [t.strip() for t in re.split(r"[,\s]+", days_blob) if t.strip()]
    days: list[Weekday] = []
    for token in day_tokens:
        if token in _WEEKDAY_ALIASES:
            days.append(_WEEKDAY_ALIASES[token])
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


def is_done(text: str) -> bool:
    return (text or "").strip().lower() in _DONE_TOKENS


def is_skip(text: str) -> bool:
    return (text or "").strip().lower() in _SKIP_TOKENS
