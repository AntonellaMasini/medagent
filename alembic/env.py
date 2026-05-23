"""Alembic environment.

Reads the database URL from our app's Settings (which loads .env) so devs
don't have to keep two URLs in sync, and exposes our model metadata so
autogenerate can detect schema drift.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make the project root importable so we can pull in our SQLAlchemy models.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_settings  # noqa: E402
from infrastructure.persistence.database import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # Calling fileConfig() does TWO disruptive things to the global logging
    # state that bite the app when alembic runs inside the FastAPI lifespan:
    #
    #   1. With disable_existing_loggers=True (the default), it flips
    #      .disabled=True on every logger that already exists (whatsapp
    #      webhook, use cases, scrapers, etc.) — all `logger.info(...)`
    #      calls become silent dead-letters.
    #
    #   2. It applies alembic.ini's `[logger_root] level = WARN` to the
    #      root logger, which downgrades the level set by main.py's
    #      basicConfig. Every INFO log from non-alembic modules is then
    #      filtered out for the rest of the process.
    #
    # We disable (1) via disable_existing_loggers=False. We work around
    # (2) by snapshotting the root level before fileConfig and restoring
    # it after — alembic still configures its own logger names, but the
    # root level the app wanted is preserved.
    _app_root_level = logging.getLogger().level
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    logging.getLogger().setLevel(_app_root_level)

# Allow override via env var (CI / programmatic invocation); else use Settings.
db_url = os.environ.get("DATABASE_URL") or get_settings().database_url
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly ALTER TABLE handling
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
