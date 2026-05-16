"""SQLite implementation of CredentialsTokenRepository."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import update

from application.setup_credentials import CredentialsToken, CredentialsTokenRepository
from infrastructure.persistence.database import CredentialsTokenRow, Database


class SQLiteCredentialsTokenRepository(CredentialsTokenRepository):
    def __init__(self, db: Database):
        self._db = db

    async def save(self, token: CredentialsToken) -> None:
        async with self._db.session() as session:
            existing = await session.get(CredentialsTokenRow, token.token)
            row = existing or CredentialsTokenRow(token=token.token)
            row.phone = token.phone
            row.expires_at = token.expires_at
            row.used_at = token.used_at
            if not row.created_at:
                row.created_at = token.created_at or datetime.utcnow()
            if existing is None:
                session.add(row)
            await session.commit()

    async def get(self, token_value: str) -> CredentialsToken | None:
        async with self._db.session() as session:
            row = await session.get(CredentialsTokenRow, token_value)
            if row is None:
                return None
            return CredentialsToken(
                token=row.token,
                phone=row.phone,
                expires_at=row.expires_at,
                used_at=row.used_at,
                created_at=row.created_at,
            )

    async def mark_used(self, token_value: str, when: datetime) -> None:
        async with self._db.session() as session:
            await session.execute(
                update(CredentialsTokenRow)
                .where(CredentialsTokenRow.token == token_value)
                .values(used_at=when)
            )
            await session.commit()

    async def revoke_for_phone(self, phone: str) -> None:
        async with self._db.session() as session:
            now = datetime.utcnow()
            await session.execute(
                update(CredentialsTokenRow)
                .where(
                    CredentialsTokenRow.phone == phone,
                    CredentialsTokenRow.used_at.is_(None),
                )
                .values(used_at=now)
            )
            await session.commit()
