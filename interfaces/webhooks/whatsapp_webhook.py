"""Twilio WhatsApp inbound webhook.

Twilio POSTs application/x-www-form-urlencoded with fields like:
  From=whatsapp:+34612345678
  To=whatsapp:+14155238886
  Body=Book me an appointment with a psychologist

We respond with an empty TwiML so Twilio doesn't auto-reply, and do real
processing in the background.

Routing precedence:
  1. If someone in this process is waiting for an OTP for this phone, treat
     the body as an OTP reply.
  2. If the phone is registered (User row exists), route to the booking flow.
  3. Otherwise, route through the onboarding state machine.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Form, Response

from application.book_appointment import AppointmentRequest, BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.onboarding import OnboardingStateMachine
from application.ports import BaseMessagingClient, BaseOTPRelay
from domain.repositories.user_repository import UserRepository
from domain.value_objects.specialty import normalize_specialty

logger = logging.getLogger(__name__)

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def build_whatsapp_router(
    user_repo: UserRepository,
    book_use_case: BookAppointmentUseCase,
    onboarding: OnboardingStateMachine,
    handle_otp: HandleOTPUseCase,
    otp_relay: BaseOTPRelay,
    whatsapp: BaseMessagingClient,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/whatsapp")
    async def whatsapp_inbound(
        background: BackgroundTasks,
        From: str = Form(...),
        Body: str = Form(default=""),
    ):
        phone = _strip_whatsapp_prefix(From)
        body = (Body or "").strip()
        logger.info("WhatsApp inbound from=%s body=%r", phone, body)

        # 1) OTP reply during an active scraper login attempt.
        if otp_relay.is_waiting(phone):
            await handle_otp.execute(phone, body)
            return _twiml()

        # 2) Registered user → booking flow.
        user = await user_repo.get_by_phone(phone)
        if user is not None:
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

        # 3) New user → onboarding state machine.
        background.add_task(onboarding.handle_message, phone, body)
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
