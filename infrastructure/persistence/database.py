"""SQLAlchemy async engine + session setup, plus declarative table models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
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
    travel_buffer_minutes: Mapped[int] = mapped_column(default=45)

    google_calendar_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OnboardingDraftRow(Base):
    """In-progress profile for a WhatsApp-onboarding user.

    Holds only non-sensitive fields. NIE + password never live here — those
    come in via the secure web form and go straight into the encrypted User
    row.
    """

    __tablename__ = "onboarding_drafts"

    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(48))
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    home_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    insurer: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preferred_times: Mapped[str | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CredentialsTokenRow(Base):
    """Single-use, time-limited token for the /setup/credentials web form.

    The token is sent over WhatsApp as a magic link. On form submission the
    server uses the token to look up the phone, decrypt the form fields, and
    persist a User. Tokens are invalidated after one use.
    """

    __tablename__ = "credentials_tokens"
    __table_args__ = (Index("ix_credentials_tokens_phone", "phone"),)

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppointmentRow(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_phone: Mapped[str] = mapped_column(String(32), index=True)
    doctor_id: Mapped[str] = mapped_column(String(128))
    doctor_name: Mapped[str] = mapped_column(String(256))
    # Spanish display name from Cigna's specialty catalog (e.g. "DERMATOLOGÍA"
    # or "PSICOLOGIA"). Snapshot at booking time — survives Cigna renames as
    # a historical record. Looked up at read time via SpecialtyCatalog.find_by_name.
    doctor_specialty_name: Mapped[str] = mapped_column(String(128))
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
