"""SQLite implementation of UserRepository with credential encryption."""
from __future__ import annotations

import json
from datetime import time

from sqlalchemy import delete, select

from domain.entities.user import Insurer, InsurerCredentials, User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.address import Address, Coordinates
from domain.value_objects.time_slot import (
    AvailabilityWindow,
    BlockedWindow,
    TimePreference,
    Weekday,
)
from infrastructure.persistence.crypto import CredentialCipher
from infrastructure.persistence.database import Database, UserRow


class SQLiteUserRepository(UserRepository):
    def __init__(self, db: Database, cipher: CredentialCipher):
        self._db = db
        self._cipher = cipher

    async def get_by_phone(self, phone: str) -> User | None:
        async with self._db.session() as session:
            row = await session.scalar(select(UserRow).where(UserRow.phone == phone))
            if row is None:
                return None
            return self._row_to_user(row)

    async def save(self, user: User) -> None:
        async with self._db.session() as session:
            existing = await session.scalar(
                select(UserRow).where(UserRow.phone == user.phone)
            )
            row = existing or UserRow(phone=user.phone)
            self._user_to_row(user, row)
            if existing is None:
                session.add(row)
            await session.commit()

    async def delete(self, phone: str) -> None:
        async with self._db.session() as session:
            await session.execute(delete(UserRow).where(UserRow.phone == phone))
            await session.commit()

    # ---- mappers ----

    def _user_to_row(self, user: User, row: UserRow) -> None:
        row.name = user.name
        row.home_address_raw = user.home_address.raw
        row.home_city = user.home_address.city
        row.home_postal_code = user.home_address.postal_code
        row.home_country = user.home_address.country
        if user.home_address.coordinates:
            row.home_lat = user.home_address.coordinates.latitude
            row.home_lng = user.home_address.coordinates.longitude
        row.insurer = user.insurer_credentials.insurer.value
        row.insurer_username_enc = self._cipher.encrypt(
            user.insurer_credentials.username
        )
        row.insurer_password_enc = self._cipher.encrypt(
            user.insurer_credentials.password
        )
        row.preferred_times = user.availability.preferred.value
        row.excluded_days_csv = ",".join(
            d.value for d in user.availability.excluded_days
        )
        row.max_weeks_out = user.availability.max_weeks_out
        row.travel_buffer_minutes = user.availability.travel_buffer_minutes
        row.blocked_windows_json = json.dumps(
            [_blocked_window_to_dict(w) for w in user.availability.blocked_windows]
        )
        row.google_calendar_token_enc = (
            self._cipher.encrypt(user.google_calendar_token)
            if user.google_calendar_token
            else None
        )

    def _row_to_user(self, row: UserRow) -> User:
        coords = None
        if row.home_lat is not None and row.home_lng is not None:
            coords = Coordinates(latitude=row.home_lat, longitude=row.home_lng)
        address = Address(
            raw=row.home_address_raw,
            city=row.home_city,
            postal_code=row.home_postal_code,
            country=row.home_country,
            coordinates=coords,
        )
        excluded = frozenset(
            Weekday(d) for d in row.excluded_days_csv.split(",") if d
        )
        blocked_windows = tuple(
            _blocked_window_from_dict(item)
            for item in json.loads(row.blocked_windows_json or "[]")
        )
        availability = AvailabilityWindow(
            preferred=TimePreference(row.preferred_times),
            excluded_days=excluded,
            max_weeks_out=row.max_weeks_out,
            travel_buffer_minutes=row.travel_buffer_minutes,
            blocked_windows=blocked_windows,
        )
        token = (
            self._cipher.decrypt(row.google_calendar_token_enc)
            if row.google_calendar_token_enc
            else None
        )
        return User(
            phone=row.phone,
            name=row.name,
            home_address=address,
            insurer_credentials=InsurerCredentials(
                insurer=Insurer(row.insurer),
                username=self._cipher.decrypt(row.insurer_username_enc),
                password=self._cipher.decrypt(row.insurer_password_enc),
            ),
            availability=availability,
            google_calendar_token=token,
            created_at=row.created_at,
        )


def _blocked_window_to_dict(window: BlockedWindow) -> dict:
    return {
        "days": sorted(d.value for d in window.days),
        "from": window.from_time.strftime("%H:%M"),
        "to": window.to_time.strftime("%H:%M"),
    }


def _blocked_window_from_dict(data: dict) -> BlockedWindow:
    return BlockedWindow(
        days=frozenset(Weekday(d) for d in data["days"]),
        from_time=_parse_hhmm(data["from"]),
        to_time=_parse_hhmm(data["to"]),
    )


def _parse_hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))
