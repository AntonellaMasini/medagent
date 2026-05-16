"""SQLite roundtrip including the new calendar-constraints fields."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from domain.entities.user import Insurer, InsurerCredentials, User
from domain.value_objects.address import Address
from domain.value_objects.time_slot import AvailabilityWindow
from infrastructure.persistence.crypto import CredentialCipher
from infrastructure.persistence.database import Database
from infrastructure.persistence.sqlite_user_repository import SQLiteUserRepository


@pytest.fixture
async def repo(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.create_all()
    cipher = CredentialCipher(Fernet.generate_key().decode())
    yield SQLiteUserRepository(db, cipher)
    await db.dispose()


def _user_with(availability: AvailabilityWindow) -> User:
    return User(
        phone="+34612345678",
        name="Test User",
        home_address=Address(raw="Calle Test 1, Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer.CIGNA, username="X12345A", password="pw"
        ),
        availability=availability,
    )


class TestSQLiteRoundtrip:
    async def test_travel_buffer_persists(self, repo):
        user = _user_with(AvailabilityWindow(travel_buffer_minutes=90))
        await repo.save(user)
        got = await repo.get_by_phone(user.phone)
        assert got is not None
        assert got.availability.travel_buffer_minutes == 90

    async def test_defaults_when_unspecified(self, repo):
        user = _user_with(AvailabilityWindow())
        await repo.save(user)
        got = await repo.get_by_phone(user.phone)
        assert got is not None
        assert got.availability.travel_buffer_minutes == 45
