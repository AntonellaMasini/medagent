"""Onboarding state machine for new WhatsApp users.

Per issue #2:
- WhatsApp collects only non-sensitive info: name, address, insurer, preferred time.
- Credentials (NIE/passport + Cigna password) are NEVER sent over WhatsApp.
  When the agent reaches the credentials step, it issues a single-use,
  time-limited token and sends the user a magic link to a HTTPS form
  (handled by interfaces/api/setup_credentials.py).
- The User row is created only after the form has been submitted
  successfully — done by SetupCredentialsUseCase, not here.

This module owns the WhatsApp half of the flow.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from application.parsers import parse_insurer, parse_preferred_time
from application.ports import BaseMessagingClient
from domain.value_objects.time_slot import TimePreference

logger = logging.getLogger(__name__)


class OnboardingState(str, Enum):
    AWAITING_NAME = "awaiting_name"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_INSURER = "awaiting_insurer"
    AWAITING_PREFERRED_TIME = "awaiting_preferred_time"
    AWAITING_CREDENTIALS = "awaiting_credentials"
    COMPLETE = "complete"


@dataclass
class OnboardingDraft:
    """In-progress profile collected over WhatsApp.

    Contains only non-sensitive fields. Credentials enter via the web form
    and go straight into the encrypted User row.
    """

    phone: str
    state: OnboardingState = OnboardingState.AWAITING_NAME
    name: str | None = None
    home_address: str | None = None
    insurer: str | None = None
    preferred_times: TimePreference | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def ready_for_credentials(self) -> bool:
        return all(
            (
                self.name,
                self.home_address,
                self.insurer,
                self.preferred_times is not None,
            )
        )


class OnboardingDraftRepository(ABC):
    @abstractmethod
    async def get_by_phone(self, phone: str) -> OnboardingDraft | None: ...

    @abstractmethod
    async def save(self, draft: OnboardingDraft) -> None: ...

    @abstractmethod
    async def delete(self, phone: str) -> None: ...


class CredentialsLinkIssuer(ABC):
    """Tiny port: when the WhatsApp flow reaches the credentials step it asks
    this collaborator for a fresh magic link to send the user."""

    @abstractmethod
    async def issue_link(self, phone: str) -> str: ...


class OnboardingStateMachine:
    """Drives a new user from first contact through profile setup."""

    def __init__(
        self,
        draft_repo: OnboardingDraftRepository,
        whatsapp: BaseMessagingClient,
        credentials_link: CredentialsLinkIssuer,
    ):
        self._draft_repo = draft_repo
        self._whatsapp = whatsapp
        self._credentials_link = credentials_link

    async def handle_message(self, phone: str, body: str) -> None:
        body = (body or "").strip()
        draft = await self._draft_repo.get_by_phone(phone)

        if draft is None:
            draft = OnboardingDraft(phone=phone)
            await self._draft_repo.save(draft)
            await self._whatsapp.send_text(phone, _PROMPTS[draft.state])
            return

        if draft.state == OnboardingState.AWAITING_CREDENTIALS:
            # User pinged us while we're waiting on the form. Re-issue a fresh
            # link in case the old one expired.
            await self._send_credentials_link(draft)
            return

        if draft.state == OnboardingState.COMPLETE:
            # Defensive: a completed draft should have been deleted. Just nudge.
            await self._whatsapp.send_text(
                phone,
                "You're all set! Just tell me when you need a doctor.",
            )
            return

        ok = await self._apply(draft, body)
        draft.updated_at = datetime.utcnow()

        if not ok:
            await self._whatsapp.send_text(phone, _RETRY[draft.state])
            await self._draft_repo.save(draft)
            return

        await self._draft_repo.save(draft)

        if draft.state == OnboardingState.AWAITING_CREDENTIALS:
            await self._send_credentials_link(draft)
        else:
            await self._whatsapp.send_text(phone, _PROMPTS[draft.state])

    # ---- step handlers ----

    async def _apply(self, draft: OnboardingDraft, body: str) -> bool:
        handler = _HANDLERS[draft.state]
        return handler(draft, body)

    async def _send_credentials_link(self, draft: OnboardingDraft) -> None:
        url = await self._credentials_link.issue_link(draft.phone)
        await self._whatsapp.send_text(
            draft.phone,
            "To connect your Cigna account securely, please enter your "
            f"details here: {url}\n\nThe link expires in 10 minutes.",
        )


# ---- handlers (free functions; bound via _HANDLERS) ----

def _handle_name(draft: OnboardingDraft, body: str) -> bool:
    if not body or len(body) < 2 or len(body) > 256:
        return False
    draft.name = body
    draft.state = OnboardingState.AWAITING_ADDRESS
    return True


def _handle_address(draft: OnboardingDraft, body: str) -> bool:
    if not body or len(body) < 5:
        return False
    draft.home_address = body
    draft.state = OnboardingState.AWAITING_INSURER
    return True


def _handle_insurer(draft: OnboardingDraft, body: str) -> bool:
    insurer = parse_insurer(body)
    if insurer is None:
        return False
    draft.insurer = insurer
    draft.state = OnboardingState.AWAITING_PREFERRED_TIME
    return True


def _handle_preferred_time(draft: OnboardingDraft, body: str) -> bool:
    pref = parse_preferred_time(body)
    if pref is None:
        return False
    draft.preferred_times = pref
    draft.state = OnboardingState.AWAITING_CREDENTIALS
    return True


_HANDLERS = {
    OnboardingState.AWAITING_NAME: _handle_name,
    OnboardingState.AWAITING_ADDRESS: _handle_address,
    OnboardingState.AWAITING_INSURER: _handle_insurer,
    OnboardingState.AWAITING_PREFERRED_TIME: _handle_preferred_time,
}


_PROMPTS: dict[OnboardingState, str] = {
    OnboardingState.AWAITING_NAME: (
        "Hi! I'll help you book private health appointments via WhatsApp. "
        "What's your full name?"
    ),
    OnboardingState.AWAITING_ADDRESS: (
        "Thanks. What's your home address? (Used to find nearby clinics — "
        "street and city is enough.)"
    ),
    OnboardingState.AWAITING_INSURER: (
        "Which private insurer are you with? (Only Cigna is supported "
        "right now.)"
    ),
    OnboardingState.AWAITING_PREFERRED_TIME: (
        "Do you prefer morning or afternoon appointments? Reply 'mornings', "
        "'afternoons', or 'any'."
    ),
    OnboardingState.AWAITING_CREDENTIALS: "",  # sent as the magic link
    OnboardingState.COMPLETE: "",
}


_RETRY: dict[OnboardingState, str] = {
    OnboardingState.AWAITING_NAME: "I didn't catch that. Please send your full name.",
    OnboardingState.AWAITING_ADDRESS: (
        "I need a slightly longer address — a street and a city work best."
    ),
    OnboardingState.AWAITING_INSURER: (
        "I only support Cigna right now. Reply 'Cigna' to continue, or send "
        "us a note if you'd like another insurer supported."
    ),
    OnboardingState.AWAITING_PREFERRED_TIME: (
        "Please reply 'mornings', 'afternoons', or 'any'."
    ),
    OnboardingState.AWAITING_CREDENTIALS: "",
    OnboardingState.COMPLETE: "",
}
