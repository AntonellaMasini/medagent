"""Centralized configuration loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Twilio ---
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_whatsapp_number: str = Field(default="whatsapp:+14155238886")
    twilio_voice_number: str = Field(default="")

    # --- Google ---
    google_maps_api_key: str = Field(default="")
    google_calendar_client_id: str = Field(default="")
    google_calendar_client_secret: str = Field(default="")

    # --- Voice (ElevenLabs Speech Engine + Twilio Voice) ---
    elevenlabs_api_key: str = Field(default="")
    elevenlabs_agent_id: str = Field(default="")  # Speech Engine agent ID (seng_...)
    elevenlabs_voice_id: str = Field(default="")
    elevenlabs_receptionist_voice_id: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    base_url_ws: str = Field(default="")  # ngrok WSS URL for media stream callbacks
    demo_mode: bool = Field(default=False)
    demo_receptionist_number: str = Field(default="")

    # --- App / storage ---
    database_url: str = Field(default="sqlite+aiosqlite:///./medagent.db")
    secret_key: str = Field(default="")  # Fernet key for credential encryption
    base_url: str = Field(default="http://localhost:8000")
    cookies_dir: Path = Field(default=PROJECT_ROOT / "infrastructure" / "cookies")

    # --- Cigna scraper ---
    cigna_login_url: str = Field(default="https://clientes.cigna.es")
    cigna_doctors_url: str = Field(default="https://clientes.cigna.es/cp/cuadro-medico")
    playwright_headless: bool = Field(default=True)
    playwright_timeout_ms: int = Field(default=30000)

    # --- Behavior ---
    otp_wait_timeout_seconds: int = Field(default=300)
    credentials_token_ttl_minutes: int = Field(default=10)
    log_level: str = Field(default="INFO")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    return _settings
