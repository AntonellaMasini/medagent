"""SQLite implementation of AppointmentRepository."""
from __future__ import annotations

from sqlalchemy import select

from domain.entities.appointment import Appointment, AppointmentStatus
from domain.entities.doctor import Doctor
from domain.repositories.appointment_repository import AppointmentRepository
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty
from domain.value_objects.time_slot import TimeSlot
from infrastructure.persistence.database import AppointmentRow, Database


class SQLiteAppointmentRepository(AppointmentRepository):
    def __init__(self, db: Database):
        self._db = db

    async def save(self, appointment: Appointment) -> None:
        async with self._db.session() as session:
            existing = await session.get(AppointmentRow, appointment.id)
            row = existing or AppointmentRow(id=appointment.id)
            self._appt_to_row(appointment, row)
            if existing is None:
                session.add(row)
            await session.commit()

    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        async with self._db.session() as session:
            row = await session.get(AppointmentRow, appointment_id)
            return self._row_to_appt(row) if row else None

    async def list_for_user(self, user_phone: str) -> list[Appointment]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(AppointmentRow)
                .where(AppointmentRow.user_phone == user_phone)
                .order_by(AppointmentRow.created_at.desc())
            )
            return [self._row_to_appt(r) for r in rows]

    def _appt_to_row(self, appt: Appointment, row: AppointmentRow) -> None:
        row.user_phone = appt.user_phone
        row.doctor_id = appt.doctor.id
        row.doctor_name = appt.doctor.name
        row.doctor_specialty = appt.doctor.specialty.value
        row.doctor_clinic_name = appt.doctor.clinic_name
        row.doctor_address = appt.doctor.address.raw
        row.doctor_phone = appt.doctor.phone
        row.doctor_distance_m = appt.doctor.distance_meters
        row.slot_start = appt.slot.start
        row.slot_duration_minutes = appt.slot.duration_minutes
        row.status = appt.status.value
        row.notes = appt.notes
        row.created_at = appt.created_at
        row.confirmed_at = appt.confirmed_at

    def _row_to_appt(self, row: AppointmentRow) -> Appointment:
        doctor = Doctor(
            id=row.doctor_id,
            name=row.doctor_name,
            specialty=Specialty(row.doctor_specialty),
            clinic_name=row.doctor_clinic_name,
            address=Address(raw=row.doctor_address),
            phone=row.doctor_phone,
            distance_meters=row.doctor_distance_m,
        )
        return Appointment(
            id=row.id,
            user_phone=row.user_phone,
            doctor=doctor,
            slot=TimeSlot(
                start=row.slot_start, duration_minutes=row.slot_duration_minutes
            ),
            status=AppointmentStatus(row.status),
            notes=row.notes,
            created_at=row.created_at,
            confirmed_at=row.confirmed_at,
        )
