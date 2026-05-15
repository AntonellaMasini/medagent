"""SQLAlchemy async engine + session setup, plus declarative table models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    home_address_raw: Mapped[str] = mapped_column(Text)
    home_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    home_postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    home_country: Mapped[str] = mapped_column(String(8), default="ES")
    home_lat: Mapped[float | None] = mapped_column(nullable=True)
    home_lng: Mapped[float | None] = mapped_column(nullable=True)

    insurer: Mapped[str] = mapped_column(String(32))
    insurer_username_enc: Mapped[str] = mapped_column(Text)
    insurer_password_enc: Mapped[str] = mapped_column(Text)

    preferred_times: Mapped[str] = mapped_column(String(16), default="any")
    excluded_days_csv: Mapped[str] = mapped_column(String(128), default="")
    max_weeks_out: Mapped[int] = mapped_column(default=4)

    google_calendar_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppointmentRow(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_phone: Mapped[str] = mapped_column(String(32), index=True)
    doctor_id: Mapped[str] = mapped_column(String(128))
    doctor_name: Mapped[str] = mapped_column(String(256))
    doctor_specialty: Mapped[str] = mapped_column(String(64))
    doctor_clinic_name: Mapped[str] = mapped_column(String(256))
    doctor_address: Mapped[str] = mapped_column(Text)
    doctor_phone: Mapped[str] = mapped_column(String(32))
    doctor_distance_m: Mapped[int | None] = mapped_column(nullable=True)

    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_duration_minutes: Mapped[int] = mapped_column(default=30)

    status: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Database:
    """Thin wrapper around the async engine + session factory."""

    def __init__(self, url: str):
        self._engine = create_async_engine(url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self._session_factory()

    async def dispose(self) -> None:
        await self._engine.dispose()
