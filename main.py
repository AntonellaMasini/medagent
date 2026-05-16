"""FastAPI app factory + dependency-injection wiring.

All concrete infrastructure is constructed here and passed into use cases.
Layers below this file see only abstract ports — no Twilio / Playwright leaks.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from application.book_appointment import BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.setup_user_profile import SetupUserProfileUseCase
from config import get_settings
from domain.entities.user import Insurer
from infrastructure.messaging.otp_relay import InMemoryOTPRelay
from infrastructure.messaging.twilio_whatsapp import TwilioWhatsAppClient
from infrastructure.persistence.crypto import CredentialCipher
from infrastructure.persistence.database import Database
from infrastructure.persistence.sqlite_appointment_repository import (
    SQLiteAppointmentRepository,
)
from infrastructure.persistence.sqlite_user_repository import SQLiteUserRepository
from infrastructure.scrapers.adeslas_scraper import AdeslasScraper
from infrastructure.scrapers.cigna_scraper import CignaScraper
from infrastructure.scrapers.multi_scraper import MultiInsurerScraper
from infrastructure.voice.twilio_voice_caller import StubVoiceCaller
from interfaces.api.health import build_health_router
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

    whatsapp = TwilioWhatsAppClient(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_number=settings.twilio_whatsapp_number,
    )
    otp_relay = InMemoryOTPRelay()
    cigna_scraper = CignaScraper(
        cookies_dir=settings.cookies_dir,
        login_url=settings.cigna_login_url,
        doctors_url=settings.cigna_doctors_url,
        headless=settings.playwright_headless,
        timeout_ms=settings.playwright_timeout_ms,
    )
    adeslas_scraper = AdeslasScraper(
        cookies_dir=settings.cookies_dir,
        login_url=settings.adeslas_login_url,
        doctors_url=settings.adeslas_doctors_url,
        headless=settings.playwright_headless,
        timeout_ms=settings.playwright_timeout_ms,
    )
    scraper = MultiInsurerScraper(
        {
            Insurer.CIGNA: cigna_scraper,
            Insurer.ADESLAS: adeslas_scraper,
        }
    )
    voice_caller = StubVoiceCaller()

    # ---- Use cases ----
    book_use_case = BookAppointmentUseCase(
        scraper=scraper,
        voice_caller=voice_caller,
        appointment_repo=appointment_repo,
        whatsapp=whatsapp,
        otp_relay=otp_relay,
        otp_timeout_seconds=settings.otp_wait_timeout_seconds,
    )
    setup_use_case = (
        SetupUserProfileUseCase(user_repo=user_repo, whatsapp=whatsapp)
        if user_repo
        else None
    )
    handle_otp = HandleOTPUseCase(otp_relay=otp_relay)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        yield
        await db.dispose()

    app = FastAPI(title="MedAgent", version="0.1.0", lifespan=lifespan)
    app.include_router(build_health_router())
    if user_repo and setup_use_case:
        app.include_router(
            build_whatsapp_router(
                user_repo=user_repo,
                book_use_case=book_use_case,
                setup_use_case=setup_use_case,
                handle_otp=handle_otp,
                whatsapp=whatsapp,
            )
        )
    return app


app = create_app()
