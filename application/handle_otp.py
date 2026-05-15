"""Use case: receive an OTP code from the user and forward it to the waiting scraper."""
from __future__ import annotations

import re

from application.ports import BaseOTPRelay

_OTP_PATTERN = re.compile(r"\b(\d{4,8})\b")


class HandleOTPUseCase:
    def __init__(self, otp_relay: BaseOTPRelay):
        self._otp_relay = otp_relay

    async def execute(self, user_phone: str, message_body: str) -> bool:
        """Extract a numeric code from the message and hand it to the relay.

        Returns True if a code was found and forwarded, False otherwise.
        """
        match = _OTP_PATTERN.search(message_body or "")
        if not match:
            return False
        await self._otp_relay.submit_code(user_phone, match.group(1))
        return True
