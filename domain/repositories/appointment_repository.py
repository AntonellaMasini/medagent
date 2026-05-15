"""Abstract appointment repository."""
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.entities.appointment import Appointment


class AppointmentRepository(ABC):
    @abstractmethod
    async def save(self, appointment: Appointment) -> None: ...

    @abstractmethod
    async def get_by_id(self, appointment_id: str) -> Appointment | None: ...

    @abstractmethod
    async def list_for_user(self, user_phone: str) -> list[Appointment]: ...
