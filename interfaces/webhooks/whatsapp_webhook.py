"""Twilio WhatsApp inbound webhook.

Twilio POSTs application/x-www-form-urlencoded with fields like:
  From=whatsapp:+34612345678
  To=whatsapp:+14155238886
  Body=Book me an appointment with a psychologist

We respond with an empty TwiML so Twilio doesn't auto-reply, and do real
processing in the background.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response

from application.book_appointment import AppointmentRequest, BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.ports import BaseMessagingClient
from application.setup_user_profile import SetupUserProfileUseCase
from domain.repositories.user_repository import UserRepository
from domain.value_objects.specialty import normalize_specialty

logger = logging.getLogger(__name__)

_EMPTY_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
)


def build_whatsapp_router(
    user_repo: UserRepository,
    book_use_case: BookAppointmentUseCase,
    setup_use_case: SetupUserProfileUseCase,
    handle_otp: HandleOTPUseCase,
    whatsapp: BaseMessagingClient,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/whatsapp")
    async def whatsapp_inbound(
        request: Request,
        background: BackgroundTasks,
        From: str = Form(...),
        Body: str = Form(default=""),
    ):
        phone = _strip_whatsapp_prefix(From)
        body = (Body or "").strip()
        logger.info("WhatsApp inbound from=%s body=%r", phone, body)

        # First: see if this looks like an OTP code we're waiting for.
        if await handle_otp.execute(phone, body):
            return _twiml()

        user = await user_repo.get_by_phone(phone)
        if user is None:
            background.add_task(setup_use_case.start, phone)
            return _twiml()

        match = normalize_specialty(body) or _extract_specialty(body)
        if match is None:
            background.add_task(
                whatsapp.send_text,
                phone,
                "I didn't recognize a specialty. Try: 'book a psychologist', "
                "'cita con dermatólogo', etc.",
            )
            return _twiml()

        req = AppointmentRequest(specialty=match.specialty, raw_query=body)
        background.add_task(book_use_case.execute, user, req)
        return _twiml()

    return router


def _twiml() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


def _strip_whatsapp_prefix(s: str) -> str:
    return s[len("whatsapp:"):] if s.startswith("whatsapp:") else s


def _extract_specialty(body: str):
    """Pull a specialty out of a freeform sentence by scanning each word."""
    for token in body.replace(",", " ").split():
        m = normalize_specialty(token)
        if m:
            return m
    return None
