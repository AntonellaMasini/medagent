"""In-memory OTP relay: webhook submits a code, scraper awaits it.

For MVP, lives in a single process. Move to Redis (pub/sub) when scaling out.
"""
from __future__ import annotations

import asyncio
import logging

from application.ports import BaseOTPRelay

logger = logging.getLogger(__name__)


class InMemoryOTPRelay(BaseOTPRelay):
    def __init__(self):
        self._futures: dict[str, asyncio.Future[str]] = {}

    async def wait_for_code(self, user_phone: str, timeout_seconds: int) -> str | None:
        loop = asyncio.get_running_loop()
        # If a code arrived before we started waiting, reuse the future.
        fut = self._futures.get(user_phone)
        if fut is None or fut.done():
            fut = loop.create_future()
            self._futures[user_phone] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("OTP wait timed out for %s", user_phone)
            self._futures.pop(user_phone, None)
            return None

    async def submit_code(self, user_phone: str, code: str) -> None:
        fut = self._futures.get(user_phone)
        if fut is None or fut.done():
            # No-one is waiting; stash it for a brief grace period.
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._futures[user_phone] = fut
        fut.set_result(code)
        logger.info("OTP code received for %s", user_phone)

    def is_waiting(self, user_phone: str) -> bool:
        fut = self._futures.get(user_phone)
        return fut is not None and not fut.done()
