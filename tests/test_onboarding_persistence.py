"""SQLite persistence roundtrip for onboarding_drafts + credentials_tokens."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from application.onboarding import OnboardingDraft, OnboardingState
from application.setup_credentials import CredentialsToken
from domain.value_objects.time_slot import TimePreference
from infrastructure.persistence.database import Database
from infrastructure.persistence.sqlite_credentials_token_repository import (
    SQLiteCredentialsTokenRepository,
)
from infrastructure.persistence.sqlite_onboarding_draft_repository import (
    SQLiteOnboardingDraftRepository,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_all()
    yield database
    await database.dispose()


class TestOnboardingDraftPersistence:
    async def test_roundtrip(self, db):
        repo = SQLiteOnboardingDraftRepository(db)
        draft = OnboardingDraft(
            phone="+34600000001",
            state=OnboardingState.AWAITING_PREFERRED_TIME,
            name="Test",
            home_address="Calle Test 1",
            insurer="cigna",
        )
        await repo.save(draft)
        got = await repo.get_by_phone("+34600000001")
        assert got is not None
        assert got.state == OnboardingState.AWAITING_PREFERRED_TIME
        assert got.name == "Test"
        assert got.insurer == "cigna"

    async def test_update_preserves_started_at(self, db):
        repo = SQLiteOnboardingDraftRepository(db)
        draft = OnboardingDraft(phone="+34600000001", name="A")
        await repo.save(draft)
        first = await repo.get_by_phone("+34600000001")
        draft.name = "B"
        draft.state = OnboardingState.AWAITING_ADDRESS
        await repo.save(draft)
        second = await repo.get_by_phone("+34600000001")
        assert second.started_at == first.started_at
        assert second.updated_at >= first.updated_at

    async def test_preferred_time_roundtrip(self, db):
        repo = SQLiteOnboardingDraftRepository(db)
        draft = OnboardingDraft(
            phone="+34600000001",
            preferred_times=TimePreference.MORNINGS,
        )
        await repo.save(draft)
        got = await repo.get_by_phone("+34600000001")
        assert got.preferred_times == TimePreference.MORNINGS

    async def test_delete(self, db):
        repo = SQLiteOnboardingDraftRepository(db)
        await repo.save(OnboardingDraft(phone="+34600000001"))
        await repo.delete("+34600000001")
        assert await repo.get_by_phone("+34600000001") is None


class TestCredentialsTokenPersistence:
    async def test_roundtrip(self, db):
        repo = SQLiteCredentialsTokenRepository(db)
        expires = datetime.utcnow() + timedelta(minutes=10)
        token = CredentialsToken(
            token="abc123", phone="+34600000001", expires_at=expires
        )
        await repo.save(token)
        got = await repo.get("abc123")
        assert got is not None
        assert got.phone == "+34600000001"
        assert abs((got.expires_at - expires).total_seconds()) < 1
        assert got.used_at is None

    async def test_mark_used(self, db):
        repo = SQLiteCredentialsTokenRepository(db)
        await repo.save(
            CredentialsToken(
                token="abc",
                phone="+34600000001",
                expires_at=datetime.utcnow() + timedelta(minutes=10),
            )
        )
        now = datetime.utcnow()
        await repo.mark_used("abc", now)
        got = await repo.get("abc")
        assert got.used_at is not None

    async def test_revoke_for_phone_marks_outstanding_only(self, db):
        repo = SQLiteCredentialsTokenRepository(db)
        now = datetime.utcnow()
        # Outstanding
        await repo.save(
            CredentialsToken(
                token="a", phone="+34600000001",
                expires_at=now + timedelta(minutes=10),
            )
        )
        # Already used
        await repo.save(
            CredentialsToken(
                token="b", phone="+34600000001",
                expires_at=now + timedelta(minutes=10),
                used_at=now - timedelta(minutes=1),
            )
        )
        # Different phone
        await repo.save(
            CredentialsToken(
                token="c", phone="+34600000002",
                expires_at=now + timedelta(minutes=10),
            )
        )

        await repo.revoke_for_phone("+34600000001")

        a = await repo.get("a")
        c = await repo.get("c")
        assert a.used_at is not None  # revoked
        assert c.used_at is None  # untouched (other phone)
