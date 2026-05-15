"""Outbound voice caller. MVP stub — does not actually dial.

v2 will use Twilio Programmable Voice + a real-time LLM (ElevenLabs / GPT-4
Realtime) to conduct conversations with clinic receptionists. For now this
stub satisfies the BaseVoiceCaller port so the orchestrator runs end-to-end
and we can develop the rest of the pipeline.
"""
from __future__ import annotations

import logging

from application.ports import BaseVoiceCaller, CallOutcome
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.time_slot import AvailabilityWindow

logger = logging.getLogger(__name__)


class StubVoiceCaller(BaseVoiceCaller):
    """No-op voice caller that pretends every call failed.

    Useful for developing the end-to-end flow without burning real phone calls.
    """

    async def book_first_available(
        self,
        doctors: list[Doctor],
        on_behalf_of: User,
        constraints: AvailabilityWindow,
    ) -> CallOutcome | None:
        for d in doctors:
            logger.info("[voice stub] would call %s (%s)", d.name, d.phone)
        return None
