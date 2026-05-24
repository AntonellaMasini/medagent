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
  2. If there's a pending gender question for this phone, treat the body
     as the gender preference reply.
  3. If the phone is registered (User row exists), route to the booking flow.
  4. Otherwise, route through the onboarding state machine.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Form, Response

from application.book_appointment import AppointmentRequest, BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.intent_parser import parse_appointment_intent
from application.onboarding import OnboardingStateMachine
from application.ports import BaseMessagingClient, BaseOTPRelay
from domain.entities.user import User
from domain.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def build_whatsapp_router(
    user_repo: UserRepository,
    book_use_case: BookAppointmentUseCase,
    onboarding: OnboardingStateMachine,
    handle_otp: HandleOTPUseCase,
    otp_relay: BaseOTPRelay,
    whatsapp: BaseMessagingClient,
    calendar_auth_url: str = "",
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    # Pending gender preference questions, keyed by phone number.
    # Stores (User, AppointmentRequest) while we wait for the user's reply.
    pending_gender: dict[str, tuple[User, AppointmentRequest]] = {}

    @router.post("/whatsapp")
    async def whatsapp_inbound(
        background: BackgroundTasks,
        From: str = Form(...),
        Body: str = Form(default=""),
    ):
        phone = _strip_whatsapp_prefix(From)
        body = (Body or "").strip()
        logger.info("WhatsApp inbound from=%s body=%r", phone, body)

        # 1) Active scraper login waiting on OTP. Submit if the body carries
        #    a code; otherwise nudge so we don't silently drop the message,
        #    and don't fall through — falling through during an active wait
        #    could spawn a second scraper session for the same user.
        if otp_relay.is_waiting(phone):
            if not await handle_otp.execute(phone, body):
                background.add_task(
                    whatsapp.send_text,
                    phone,
                    "Still waiting for your Cigna code — please reply with "
                    "just the digits from the SMS.",
                )
            return _twiml()

        # 2) Pending gender preference question for this phone.
        if phone in pending_gender:
            user, req = pending_gender.pop(phone)
            gender = _parse_gender_reply(body)
            if gender == "skip":
                background.add_task(book_use_case.execute, user, req)
            else:
                req = _with_gender(req, gender)
                background.add_task(book_use_case.execute, user, req)
            return _twiml()

        # 3) Registered user → booking flow.
        user = await user_repo.get_by_phone(phone)
        if user is not None:
            # Handle "connect calendar" command
            if _is_calendar_command(body) and calendar_auth_url:
                from urllib.parse import quote

                link = f"{calendar_auth_url}?phone={quote(phone, safe='')}"
                background.add_task(
                    whatsapp.send_text,
                    phone,
                    f"Connect your Google Calendar so I can check your "
                    f"availability:\n{link}",
                )
                return _twiml()

            req = parse_appointment_intent(body)
            if req is None:
                background.add_task(
                    whatsapp.send_text,
                    phone,
                    "I didn't recognize a specialty. Try: 'book a psicologo', "
                    "'I need a dermatologo', etc.",
                )
                return _twiml()

            # Ask gender preference before proceeding
            pending_gender[phone] = (user, req)
            background.add_task(
                whatsapp.send_text,
                phone,
                "Do you have a preference for the doctor's gender?\n"
                'Reply: "female", "male", or "no preference"',
            )
            return _twiml()

        # 3) New user → onboarding state machine.
        background.add_task(onboarding.handle_message, phone, body)
        return _twiml()

    return router


def _twiml() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


def _strip_whatsapp_prefix(s: str) -> str:
    return s[len("whatsapp:"):] if s.startswith("whatsapp:") else s


_CALENDAR_KEYWORDS = {"calendar", "calendario", "connect calendar", "conectar calendario"}


def _is_calendar_command(body: str) -> bool:
    """Check if the message is a request to connect Google Calendar."""
    lower = body.lower().strip()
    return lower in _CALENDAR_KEYWORDS


_FEMALE_KEYWORDS: tuple[str, ...] = (
    "female", "woman", "doctora", "mujer", "f",
)
_MALE_KEYWORDS: tuple[str, ...] = (
    "male", "man", "doctor", "hombre", "m",
)
_NO_PREF_KEYWORDS: tuple[str, ...] = (
    "no", "no preference", "sin preferencia", "da igual",
    "cualquiera", "indiferente", "either", "any",
)


def _parse_gender_reply(body: str) -> str | None:
    """Interpret the user's reply to the gender preference question.

    Returns 'female', 'male', 'skip' (no preference), or None.
    """
    lower = body.lower().strip()
    if any(kw == lower or kw in lower.split() for kw in _FEMALE_KEYWORDS):
        return "female"
    if any(kw == lower or kw in lower.split() for kw in _MALE_KEYWORDS):
        return "male"
    if any(kw in lower for kw in _NO_PREF_KEYWORDS):
        return "skip"
    # Default: treat unrecognized reply as no preference
    return "skip"


def _with_gender(req: AppointmentRequest, gender: str | None) -> AppointmentRequest:
    """Return a copy of the request with gender_preference set."""
    import dataclasses

    if gender and gender != "skip":
        return dataclasses.replace(req, gender_preference=gender)
    return req
