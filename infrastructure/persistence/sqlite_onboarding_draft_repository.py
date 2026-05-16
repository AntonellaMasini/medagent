"""SQLite implementation of OnboardingDraftRepository."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select

from application.onboarding import (
    OnboardingDraft,
    OnboardingDraftRepository,
    OnboardingState,
)
from domain.value_objects.time_slot import TimePreference
from infrastructure.persistence.database import Database, OnboardingDraftRow


class SQLiteOnboardingDraftRepository(OnboardingDraftRepository):
    def __init__(self, db: Database):
        self._db = db

    async def get_by_phone(self, phone: str) -> OnboardingDraft | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(OnboardingDraftRow).where(OnboardingDraftRow.phone == phone)
            )
            if row is None:
                return None
            return self._row_to_draft(row)

    async def save(self, draft: OnboardingDraft) -> None:
        async with self._db.session() as session:
            existing = await session.get(OnboardingDraftRow, draft.phone)
            row = existing or OnboardingDraftRow(phone=draft.phone)
            self._draft_to_row(draft, row)
            if existing is None:
                session.add(row)
            await session.commit()

    async def delete(self, phone: str) -> None:
        async with self._db.session() as session:
            await session.execute(
                delete(OnboardingDraftRow).where(OnboardingDraftRow.phone == phone)
            )
            await session.commit()

    def _draft_to_row(self, draft: OnboardingDraft, row: OnboardingDraftRow) -> None:
        row.state = draft.state.value
        row.name = draft.name
        row.home_address = draft.home_address
        row.insurer = draft.insurer
        row.preferred_times = (
            draft.preferred_times.value if draft.preferred_times else None
        )
        if not row.started_at:
            row.started_at = draft.started_at
        row.updated_at = datetime.utcnow()

    def _row_to_draft(self, row: OnboardingDraftRow) -> OnboardingDraft:
        return OnboardingDraft(
            phone=row.phone,
            state=OnboardingState(row.state),
            name=row.name,
            home_address=row.home_address,
            insurer=row.insurer,
            preferred_times=(
                TimePreference(row.preferred_times) if row.preferred_times else None
            ),
            started_at=row.started_at,
            updated_at=row.updated_at,
        )
