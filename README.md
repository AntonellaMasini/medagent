# MedAgent

WhatsApp-driven AI agent that books private health insurance appointments in Spain.
User sends *"book me a psychologist"* → agent logs into the insurer's portal, finds nearby doctors, calls clinics until a slot is confirmed, and replies with the booking.

First insurer adapter: **Cigna Spain**.

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

# Run (migrations run automatically on boot in dev)
uv run uvicorn main:app --reload
```

## Database migrations

Alembic owns the schema. The FastAPI lifespan runs `alembic upgrade head` on boot for dev convenience; in production, run it as a deploy step and drop the lifespan call.

```bash
# Apply pending migrations manually
uv run alembic upgrade head

# After changing a SQLAlchemy model in infrastructure/persistence/database.py:
uv run alembic revision --autogenerate -m "what changed"
# Review the generated file in alembic/versions/ before committing.

# Roll back the last migration
uv run alembic downgrade -1
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
