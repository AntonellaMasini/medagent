"""Appointment entity."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import TimeSlot


class AppointmentStatus(str, Enum):
    PENDING = "pending"        # search in progress
    CONFIRMED = "confirmed"    # clinic confirmed slot
    FAILED = "failed"          # no slot found across all clinics
    CANCELLED = "cancelled"


@dataclass
class Appointment:
    """A booking attempt and its outcome."""

    id: str
    user_phone: str
    doctor: Doctor
    slot: TimeSlot
    status: AppointmentStatus
    created_at: datetime = field(default_factory=datetime.utcnow)
    confirmed_at: datetime | None = None
    notes: str | None = None

    @classmethod
    def create(cls, user: User, doctor: Doctor, slot: TimeSlot) -> "Appointment":
        return cls(
            id=str(uuid.uuid4()),
            user_phone=user.phone,
            doctor=doctor,
            slot=slot,
            status=AppointmentStatus.CONFIRMED,
            confirmed_at=datetime.utcnow(),
        )
