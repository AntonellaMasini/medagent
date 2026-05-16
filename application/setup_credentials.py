"""Token-gated credential intake: the web-form half of onboarding.

Flow:
  1. State machine reaches AWAITING_CREDENTIALS → calls `issue_link(phone)`.
     A random token is generated, stored with a 10-min TTL, and embedded in
     a URL that the WhatsApp client sends to the user.
  2. User clicks the link. GET /setup/credentials?token=… looks up the token
     via `validate_token`, refuses if invalid/expired/used, otherwise serves
     the credentials form.
  3. User submits NIE + password. POST /setup/credentials calls
     `submit_credentials(...)`. On success the User row is created with
     credentials encrypted at rest, the draft is deleted, the token is marked
     used, and the user receives a confirmation WhatsApp message.
"""
from __future__ import annotations

import logging
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from application.onboarding import (
    CredentialsLinkIssuer,
    OnboardingDraft,
    OnboardingDraftRepository,
    OnboardingState,
)
from application.ports import BaseMessagingClient
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.address import Address
from domain.value_objects.time_slot import AvailabilityWindow, TimePreference

logger = logging.getLogger(__name__)


@dataclass
class CredentialsToken:
    token: str
    phone: str
    expires_at: datetime
    used_at: datetime | None = None
    created_at: datetime | None = None

    def is_valid(self, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        return self.used_at is None and now < self.expires_at


class TokenStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


@dataclass
class TokenValidation:
    status: TokenStatus
    phone: str | None = None
    draft: OnboardingDraft | None = None


class SubmissionResult(str, Enum):
    OK = "ok"
    INVALID_TOKEN = "invalid_token"
    EXPIRED_TOKEN = "expired_token"
    USED_TOKEN = "used_token"
    DRAFT_MISSING = "draft_missing"
    BAD_INPUT = "bad_input"


@dataclass
class SubmissionOutcome:
    result: SubmissionResult
    user: User | None = None


class CredentialsTokenRepository(ABC):
    @abstractmethod
    async def save(self, token: CredentialsToken) -> None: ...

    @abstractmethod
    async def get(self, token_value: str) -> CredentialsToken | None: ...

    @abstractmethod
    async def mark_used(self, token_value: str, when: datetime) -> None: ...

    @abstractmethod
    async def revoke_for_phone(self, phone: str) -> None:
        """Mark all outstanding tokens for `phone` as used (i.e. invalidated).

        Called whenever a new token is issued so only the latest link works.
        """


class SetupCredentialsUseCase(CredentialsLinkIssuer):
    """Owns the lifecycle of the credentials magic link.

    Implements `CredentialsLinkIssuer` so the onboarding state machine can
    request a link without a dependency on the persistence layer.
    """

    def __init__(
        self,
        token_repo: CredentialsTokenRepository,
        draft_repo: OnboardingDraftRepository,
        user_repo: UserRepository,
        whatsapp: BaseMessagingClient,
        base_url: str,
        token_ttl_minutes: int = 10,
        path: str = "/setup/credentials",
    ):
        self._token_repo = token_repo
        self._draft_repo = draft_repo
        self._user_repo = user_repo
        self._whatsapp = whatsapp
        self._base_url = base_url.rstrip("/")
        self._token_ttl_minutes = token_ttl_minutes
        self._path = path

    # ---- CredentialsLinkIssuer ----

    async def issue_link(self, phone: str) -> str:
        await self._token_repo.revoke_for_phone(phone)
        token = CredentialsToken(
            token=secrets.token_urlsafe(32),
            phone=phone,
            expires_at=datetime.utcnow() + timedelta(minutes=self._token_ttl_minutes),
            created_at=datetime.utcnow(),
        )
        await self._token_repo.save(token)
        logger.info(
            "Issued credentials token for %s (expires in %d min)",
            phone,
            self._token_ttl_minutes,
        )
        return f"{self._base_url}{self._path}?token={token.token}"

    # ---- GET handler support ----

    async def validate_token(self, token_value: str) -> TokenValidation:
        token = await self._token_repo.get(token_value)
        if token is None:
            return TokenValidation(status=TokenStatus.NOT_FOUND)
        if token.used_at is not None:
            return TokenValidation(status=TokenStatus.ALREADY_USED)
        if not token.is_valid():
            return TokenValidation(status=TokenStatus.EXPIRED)
        draft = await self._draft_repo.get_by_phone(token.phone)
        return TokenValidation(status=TokenStatus.OK, phone=token.phone, draft=draft)

    # ---- POST handler ----

    async def submit_credentials(
        self,
        token_value: str,
        nie: str,
        password: str,
    ) -> SubmissionOutcome:
        nie = (nie or "").strip().upper()
        password = (password or "").strip()
        if not nie or not password:
            return SubmissionOutcome(result=SubmissionResult.BAD_INPUT)

        validation = await self.validate_token(token_value)
        if validation.status == TokenStatus.NOT_FOUND:
            return SubmissionOutcome(result=SubmissionResult.INVALID_TOKEN)
        if validation.status == TokenStatus.EXPIRED:
            return SubmissionOutcome(result=SubmissionResult.EXPIRED_TOKEN)
        if validation.status == TokenStatus.ALREADY_USED:
            return SubmissionOutcome(result=SubmissionResult.USED_TOKEN)

        draft = validation.draft
        if draft is None or not draft.ready_for_credentials:
            return SubmissionOutcome(result=SubmissionResult.DRAFT_MISSING)

        user = _build_user(draft, nie=nie, password=password)
        await self._user_repo.save(user)
        await self._token_repo.mark_used(token_value, datetime.utcnow())
        await self._draft_repo.delete(draft.phone)

        await self._whatsapp.send_text(
            draft.phone,
            "You're all set! Just tell me when you need a doctor 🙂",
        )
        logger.info("Onboarding complete for %s", draft.phone)
        return SubmissionOutcome(result=SubmissionResult.OK, user=user)


def _build_user(draft: OnboardingDraft, *, nie: str, password: str) -> User:
    if draft.state != OnboardingState.AWAITING_CREDENTIALS:
        raise ValueError(
            f"draft in state {draft.state.value} is not ready for credentials"
        )
    assert draft.name and draft.home_address and draft.insurer
    return User(
        phone=draft.phone,
        name=draft.name,
        home_address=Address(raw=draft.home_address),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer(draft.insurer),
            username=nie,
            password=password,
        ),
        availability=AvailabilityWindow(
            preferred=draft.preferred_times or TimePreference.ANY,
        ),
    )
