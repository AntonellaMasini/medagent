"""Programmatic Alembic invocation.

The CLI is the source of truth for migrations (alembic upgrade head). This
module just lets the FastAPI lifespan run the same upgrade at boot in dev
so contributors don't forget. For prod, run the CLI as a deploy step and
disable the autoupgrade.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def upgrade_head(database_url: str | None = None) -> None:
    """Run `alembic upgrade head` synchronously.

    If `database_url` is given, it overrides whatever alembic/env.py would
    resolve via Settings — useful for tests / one-off invocations.
    """
    cfg = Config(str(ALEMBIC_INI))
    if database_url:
        # env.py reads from os.environ['DATABASE_URL'] when set.
        import os

        os.environ["DATABASE_URL"] = database_url
    logger.info("Running alembic upgrade head…")
    command.upgrade(cfg, "head")
    logger.info("Alembic upgrade complete.")
