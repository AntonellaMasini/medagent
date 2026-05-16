"""SetupCredentialsUseCase: token lifecycle + web-form submission."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from application.onboarding import (
    OnboardingDraft,
    OnboardingDraftRepository,
    OnboardingState,
)
from application.ports import BaseMessagingClient
from application.setup_credentials import (
    CredentialsToken,
    CredentialsTokenRepository,
    SetupCredentialsUseCase,
    SubmissionResult,
    TokenStatus,
)
from domain.entities.appointment import Appointment
from domain.entities.user import User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.time_slot import TimePreference


# ---- fakes ----

class FakeWhatsApp(BaseMessagingClient):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to_phone: str, body: str) -> None:
        self.sent.append((to_phone, body))

    async def send_confirmation(self, user: User, appointment: Appointment) -> None:
        pass

    async def request_otp(self, user: User) -> None:
        pass


@dataclass
class InMemoryUserRepo(UserRepository):
    users: dict[str, User] = field(default_factory=dict)

    async def get_by_phone(self, phone: str) -> User | None:
        return self.users.get(phone)

    async def save(self, user: User) -> None:
        self.users[user.phone] = user

    async def delete(self, phone: str) -> None:
        self.users.pop(phone, None)


@dataclass
class InMemoryDraftRepo(OnboardingDraftRepository):
    drafts: dict[str, OnboardingDraft] = field(default_factory=dict)

    async def get_by_phone(self, phone: str) -> OnboardingDraft | None:
        return self.drafts.get(phone)

    async def save(self, draft: OnboardingDraft) -> None:
        self.drafts[draft.phone] = draft

    async def delete(self, phone: str) -> None:
        self.drafts.pop(phone, None)


@dataclass
class InMemoryTokenRepo(CredentialsTokenRepository):
    tokens: dict[str, CredentialsToken] = field(default_factory=dict)

    async def save(self, token: CredentialsToken) -> None:
        self.tokens[token.token] = token

    async def get(self, token_value: str) -> CredentialsToken | None:
        return self.tokens.get(token_value)

    async def mark_used(self, token_value: str, when: datetime) -> None:
        tok = self.tokens.get(token_value)
        if tok:
            tok.used_at = when

    async def revoke_for_phone(self, phone: str) -> None:
        now = datetime.utcnow()
        for tok in self.tokens.values():
            if tok.phone == phone and tok.used_at is None:
                tok.used_at = now


def _ready_draft(phone: str = "+34600000001") -> OnboardingDraft:
    return OnboardingDraft(
        phone=phone,
        state=OnboardingState.AWAITING_CREDENTIALS,
        name="Antonella Masini",
        home_address="Calle Donoso 20, Madrid",
        insurer="cigna",
        preferred_times=TimePreference.AFTERNOONS,
    )


@pytest.fixture
def setup():
    wa = FakeWhatsApp()
    users = InMemoryUserRepo()
    drafts = InMemoryDraftRepo()
    tokens = InMemoryTokenRepo()
    uc = SetupCredentialsUseCase(
        token_repo=tokens,
        draft_repo=drafts,
        user_repo=users,
        whatsapp=wa,
        base_url="https://medagent.test",
        token_ttl_minutes=10,
    )
    return uc, wa, users, drafts, tokens


# ---- issue_link ----

class TestIssueLink:
    async def test_link_format(self, setup):
        uc, _, _, _, tokens = setup
        url = await uc.issue_link("+34600000001")
        assert url.startswith("https://medagent.test/setup/credentials?token=")
        # the token in the URL is stored in the repo
        token_value = url.split("token=")[1]
        assert token_value in tokens.tokens
        assert tokens.tokens[token_value].phone == "+34600000001"

    async def test_reissue_revokes_previous_token(self, setup):
        uc, _, _, _, tokens = setup
        phone = "+34600000001"
        url1 = await uc.issue_link(phone)
        url2 = await uc.issue_link(phone)
        tok1 = url1.split("token=")[1]
        tok2 = url2.split("token=")[1]
        assert tokens.tokens[tok1].used_at is not None  # revoked
        assert tokens.tokens[tok2].used_at is None  # still active


# ---- validate_token ----

class TestValidateToken:
    async def test_unknown_token(self, setup):
        uc, *_ = setup
        result = await uc.validate_token("nope")
        assert result.status == TokenStatus.NOT_FOUND

    async def test_expired_token(self, setup):
        uc, _, _, _, tokens = setup
        await tokens.save(
            CredentialsToken(
                token="t",
                phone="+34600000001",
                expires_at=datetime.utcnow() - timedelta(minutes=1),
            )
        )
        result = await uc.validate_token("t")
        assert result.status == TokenStatus.EXPIRED

    async def test_already_used_token(self, setup):
        uc, _, _, _, tokens = setup
        await tokens.save(
            CredentialsToken(
                token="t",
                phone="+34600000001",
                expires_at=datetime.utcnow() + timedelta(minutes=5),
                used_at=datetime.utcnow(),
            )
        )
        result = await uc.validate_token("t")
        assert result.status == TokenStatus.ALREADY_USED

    async def test_valid_token_returns_phone_and_draft(self, setup):
        uc, _, _, drafts, _ = setup
        draft = _ready_draft()
        await drafts.save(draft)
        url = await uc.issue_link(draft.phone)
        token = url.split("token=")[1]
        result = await uc.validate_token(token)
        assert result.status == TokenStatus.OK
        assert result.phone == draft.phone
        assert result.draft is not None


# ---- submit_credentials ----

class TestSubmitCredentials:
    async def test_happy_path_creates_user_and_cleans_up(self, setup):
        uc, wa, users, drafts, tokens = setup
        draft = _ready_draft()
        await drafts.save(draft)
        url = await uc.issue_link(draft.phone)
        token = url.split("token=")[1]

        outcome = await uc.submit_credentials(
            token_value=token, nie="x12345a", password="hunter2"
        )

        assert outcome.result == SubmissionResult.OK
        assert draft.phone in users.users
        saved = users.users[draft.phone]
        assert saved.insurer_credentials.username == "X12345A"  # upcased
        assert saved.insurer_credentials.password == "hunter2"
        assert saved.availability.preferred == TimePreference.AFTERNOONS
        assert draft.phone not in drafts.drafts  # draft deleted
        assert tokens.tokens[token].used_at is not None
        assert any("all set" in body.lower() for _, body in wa.sent)

    async def test_missing_fields_rejected(self, setup):
        uc, _, _, drafts, _ = setup
        draft = _ready_draft()
        await drafts.save(draft)
        url = await uc.issue_link(draft.phone)
        token = url.split("token=")[1]
        outcome = await uc.submit_credentials(token, nie="", password="x")
        assert outcome.result == SubmissionResult.BAD_INPUT
        outcome = await uc.submit_credentials(token, nie="x", password="")
        assert outcome.result == SubmissionResult.BAD_INPUT

    async def test_expired_token_rejected(self, setup):
        uc, _, _, drafts, tokens = setup
        draft = _ready_draft()
        await drafts.save(draft)
        await tokens.save(
            CredentialsToken(
                token="t",
                phone=draft.phone,
                expires_at=datetime.utcnow() - timedelta(seconds=1),
            )
        )
        outcome = await uc.submit_credentials("t", nie="X12345A", password="pw")
        assert outcome.result == SubmissionResult.EXPIRED_TOKEN

    async def test_replay_after_use_rejected(self, setup):
        uc, _, users, drafts, _ = setup
        draft = _ready_draft()
        await drafts.save(draft)
        url = await uc.issue_link(draft.phone)
        token = url.split("token=")[1]
        first = await uc.submit_credentials(token, nie="X12345A", password="pw")
        assert first.result == SubmissionResult.OK

        # Replay with the same token after success
        replay = await uc.submit_credentials(token, nie="X12345A", password="pw")
        assert replay.result == SubmissionResult.USED_TOKEN

    async def test_draft_missing_rejected(self, setup):
        uc, _, _, _, _ = setup
        # Issue a token for a phone that has no draft
        url = await uc.issue_link("+34600000001")
        token = url.split("token=")[1]
        outcome = await uc.submit_credentials(token, nie="X12345A", password="pw")
        assert outcome.result == SubmissionResult.DRAFT_MISSING

    async def test_unknown_token_rejected(self, setup):
        uc, *_ = setup
        outcome = await uc.submit_credentials(
            "totally-bogus", nie="X12345A", password="pw"
        )
        assert outcome.result == SubmissionResult.INVALID_TOKEN
