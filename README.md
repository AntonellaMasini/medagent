# MedAgent

WhatsApp-driven AI agent that books private health insurance appointments in Spain.
User sends *"book me a psychologist"* → agent logs into the insurer's portal, finds nearby doctors, calls clinics until a slot is confirmed, and replies with the booking.

Insurer adapters: **Cigna Spain**, **Adeslas**.

## Status

MVP scaffold. WhatsApp webhook + Cigna scraper are the first runnable pieces; voice calling currently stubbed.

## Stack

Python 3.11+ · FastAPI · Playwright · Twilio (WhatsApp + Voice) · SQLite (will become Postgres) · uv for dep management.

## Architecture

Domain-Driven Design with strict layer dependencies (inward-only):

```
interfaces/  → application/  → domain/
                  ↑
            infrastructure/  (implements domain ports)
```

- `domain/` — entities, value objects, repository interfaces. Pure Python.
- `application/` — use cases. Orchestrates domain via abstract ports.
- `infrastructure/` — Playwright, Twilio, SQLite, Google APIs.
- `interfaces/` — FastAPI routes and webhooks.

## Setup

```bash
# Install deps
uv sync
uv run playwright install chromium

# Configure
cp .env.example .env
# fill in Twilio creds, generate SECRET_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Run
uv run uvicorn main:app --reload
```

Then expose with `ngrok http 8000` and point your Twilio WhatsApp sandbox webhook at `https://<ngrok-id>.ngrok-free.app/webhooks/whatsapp`.

## Layout

```
main.py                  # FastAPI factory + DI container
config.py                # pydantic-settings env config

domain/                  # entities, value objects, repository ABCs
application/             # use cases + outbound ports
infrastructure/          # scrapers, voice, messaging, persistence, external APIs
interfaces/              # webhooks + HTTP API
```

See `medagent_project_brief.md` for the full spec.
