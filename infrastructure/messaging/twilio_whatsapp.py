"""Twilio WhatsApp messaging client."""
from __future__ import annotations

import asyncio
import logging

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from application.ports import BaseMessagingClient
from domain.entities.appointment import Appointment, AppointmentStatus
from domain.entities.user import User

logger = logging.getLogger(__name__)


class TwilioWhatsAppClient(BaseMessagingClient):
    """Sends outbound WhatsApp messages via Twilio.

    The Twilio SDK is sync; we run calls in a worker thread so we don't block
    the FastAPI event loop.
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        if not account_sid or not auth_token:
            logger.warning(
                "Twilio credentials missing — WhatsApp client will no-op. "
                "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env."
            )
            self._client = None
        else:
            self._client = TwilioClient(account_sid, auth_token)
        self._from = _ensure_whatsapp_prefix(from_number)

    async def send_text(self, to_phone: str, body: str) -> None:
        to = _ensure_whatsapp_prefix(to_phone)
        if self._client is None:
            logger.info("[whatsapp:noop] to=%s body=%s", to, body)
            return
        await asyncio.to_thread(self._send, to, body)

    async def send_confirmation(self, user: User, appointment: Appointment) -> None:
        body = _format_confirmation(appointment)
        await self.send_text(user.phone, body)

    async def request_otp(self, user: User) -> None:
        await self.send_text(
            user.phone,
            "Cigna sent you a verification code by SMS. "
            "Reply with the code (just the digits) to continue.",
        )

    def _send(self, to: str, body: str) -> None:
        try:
            msg = self._client.messages.create(  # type: ignore[union-attr]
                from_=self._from, to=to, body=body
            )
            logger.info("Sent WhatsApp message sid=%s to=%s", msg.sid, to)
        except TwilioRestException as e:
            logger.error("Twilio send failed: %s", e)
            raise


def _ensure_whatsapp_prefix(number: str) -> str:
    if not number:
        return number
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def _format_confirmation(appt: Appointment) -> str:
    if appt.status != AppointmentStatus.CONFIRMED:
        return (
            f"Search complete. Status: {appt.status.value}. "
            f"{appt.notes or ''}".strip()
        )
    when = appt.slot.start.strftime("%a %d %b at %H:%M")
    return (
        f"Appointment booked.\n"
        f"Doctor: {appt.doctor.name}\n"
        f"Specialty: {appt.doctor.specialty.value.title()}\n"
        f"When: {when}\n"
        f"Where: {appt.doctor.clinic_name}, {appt.doctor.address.raw}\n"
        f"Phone: {appt.doctor.phone}"
    )
