"""FastAPI app factory + dependency-injection wiring.

All concrete infrastructure is constructed here and passed into use cases.
Layers below this file see only abstract ports — no Twilio / Playwright leaks.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from application.book_appointment import BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.onboarding import OnboardingStateMachine
from application.setup_credentials import SetupCredentialsUseCase
from config import get_settings
from infrastructure.messaging.otp_relay import InMemoryOTPRelay
from infrastructure.messaging.twilio_whatsapp import TwilioWhatsAppClient
from infrastructure.persistence.crypto import CredentialCipher
from infrastructure.persistence.database import Database
from infrastructure.persistence.migrations import upgrade_head
from infrastructure.persistence.sqlite_appointment_repository import (
    SQLiteAppointmentRepository,
)
from infrastructure.persistence.sqlite_credentials_token_repository import (
    SQLiteCredentialsTokenRepository,
)
from infrastructure.persistence.sqlite_onboarding_draft_repository import (
    SQLiteOnboardingDraftRepository,
)
from infrastructure.persistence.sqlite_user_repository import SQLiteUserRepository
from infrastructure.scrapers.cigna_scraper import CignaScraper
from infrastructure.calendar.google_calendar_service import GoogleCalendarService
from infrastructure.voice.elevenlabs_voice_caller import (
    ElevenLabsVoiceCaller,
    VoiceCallerConfig,
)
from infrastructure.voice.twilio_voice_caller import StubVoiceCaller
from interfaces.api.google_auth import build_google_auth_router
from interfaces.api.health import build_health_router
from interfaces.api.setup_credentials import build_setup_credentials_router
from interfaces.webhooks.voice_webhook import router as voice_router
from interfaces.webhooks.whatsapp_webhook import build_whatsapp_router


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    # ---- Infrastructure ----
    db = Database(settings.database_url)
    cipher = CredentialCipher(settings.secret_key) if settings.secret_key else None
    if cipher is None:
        logging.getLogger(__name__).warning(
            "SECRET_KEY not set — user persistence will fail until configured."
        )

    user_repo = SQLiteUserRepository(db, cipher) if cipher else None
    appointment_repo = SQLiteAppointmentRepository(db)
    draft_repo = SQLiteOnboardingDraftRepository(db)
    token_repo = SQLiteCredentialsTokenRepository(db)

    whatsapp = TwilioWhatsAppClient(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_number=settings.twilio_whatsapp_number,
    )
    otp_relay = InMemoryOTPRelay()
    scraper = CignaScraper(
        cookies_dir=settings.cookies_dir,
        login_url=settings.cigna_login_url,
        doctors_url=settings.cigna_doctors_url,
        headless=settings.playwright_headless,
        timeout_ms=settings.playwright_timeout_ms,
    )
    # Google Calendar service (optional — needs client_id + secret)
    calendar_service: GoogleCalendarService | None = None
    if settings.google_calendar_client_id and settings.google_calendar_client_secret:
        calendar_service = GoogleCalendarService(
            client_id=settings.google_calendar_client_id,
            client_secret=settings.google_calendar_client_secret,
            redirect_uri=f"{settings.base_url}/auth/google/callback",
        )

    # Use real voice caller if ElevenLabs Speech Engine is configured.
    if settings.elevenlabs_api_key and settings.elevenlabs_agent_id:
        voice_caller = ElevenLabsVoiceCaller(
            config=VoiceCallerConfig(
                elevenlabs_api_key=settings.elevenlabs_api_key,
                elevenlabs_agent_id=settings.elevenlabs_agent_id,
                elevenlabs_phone_number_id=settings.elevenlabs_phone_number_id,
                anthropic_api_key=settings.anthropic_api_key,
                demo_mode=settings.demo_mode,
                demo_receptionist_number=settings.demo_receptionist_number,
            )
        )
    else:
        voice_caller = StubVoiceCaller()

    # ---- Use cases ----
    cal_auth_url = (
        f"{settings.base_url}/auth/google/start"
        if calendar_service
        else ""
    )
    book_use_case = BookAppointmentUseCase(
        scraper=scraper,
        voice_caller=voice_caller,
        appointment_repo=appointment_repo,
        whatsapp=whatsapp,
        otp_relay=otp_relay,
        calendar=calendar_service,
        calendar_auth_url=cal_auth_url,
        otp_timeout_seconds=settings.otp_wait_timeout_seconds,
    )
    handle_otp = HandleOTPUseCase(otp_relay=otp_relay)
    setup_credentials = (
        SetupCredentialsUseCase(
            token_repo=token_repo,
            draft_repo=draft_repo,
            user_repo=user_repo,
            whatsapp=whatsapp,
            base_url=settings.base_url,
            token_ttl_minutes=settings.credentials_token_ttl_minutes,
        )
        if user_repo
        else None
    )
    onboarding = (
        OnboardingStateMachine(
            draft_repo=draft_repo,
            whatsapp=whatsapp,
            credentials_link=setup_credentials,
        )
        if setup_credentials
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(upgrade_head, settings.database_url)
        yield
        await db.dispose()

    app = FastAPI(title="MedAgent", version="0.1.0", lifespan=lifespan)
    app.include_router(build_health_router())
    app.include_router(voice_router)
    if user_repo and onboarding and setup_credentials:
        app.include_router(
            build_whatsapp_router(
                user_repo=user_repo,
                book_use_case=book_use_case,
                onboarding=onboarding,
                handle_otp=handle_otp,
                otp_relay=otp_relay,
                whatsapp=whatsapp,
                calendar_auth_url=cal_auth_url,
            )
        )
        app.include_router(build_setup_credentials_router(setup_credentials))
        if calendar_service:
            app.include_router(
                build_google_auth_router(
                    calendar_service=calendar_service,
                    user_repo=user_repo,
                )
            )
    return app


app = create_app()
