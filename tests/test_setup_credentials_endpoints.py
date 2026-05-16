"""Integration tests for the /setup/credentials GET and POST endpoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
)
from domain.entities.appointment import Appointment
from domain.entities.user import User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.time_slot import TimePreference
from interfaces.api.setup_credentials import build_setup_credentials_router


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
        for tok in self.tokens.values():
            if tok.phone == phone and tok.used_at is None:
                tok.used_at = datetime.utcnow()


@pytest.fixture
def client_and_uc():
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
    app = FastAPI()
    app.include_router(build_setup_credentials_router(uc))
    return TestClient(app), uc, users, drafts


def _ready_draft(phone: str = "+34600000001") -> OnboardingDraft:
    return OnboardingDraft(
        phone=phone,
        state=OnboardingState.AWAITING_CREDENTIALS,
        name="Antonella Masini",
        home_address="Calle Donoso 20, Madrid",
        insurer="cigna",
        preferred_times=TimePreference.AFTERNOONS,
    )


class TestGetForm:
    async def test_valid_token_renders_form(self, client_and_uc):
        client, uc, _, drafts = client_and_uc
        await drafts.save(_ready_draft())
        url = await uc.issue_link("+34600000001")
        token = url.split("token=")[1]

        r = client.get(f"/setup/credentials?token={token}")
        assert r.status_code == 200
        body = r.text
        assert "Connect your Cigna account" in body
        assert 'name="nie"' in body
        assert 'name="password"' in body
        assert token in body  # echoed in hidden form field
        assert "Antonella" in body  # personalized greeting

    async def test_missing_token_400(self, client_and_uc):
        client, *_ = client_and_uc
        r = client.get("/setup/credentials")
        assert r.status_code == 422  # FastAPI validation rejects missing query

    async def test_unknown_token_400(self, client_and_uc):
        client, *_ = client_and_uc
        r = client.get("/setup/credentials?token=does-not-exist-1234")
        assert r.status_code == 400
        # HTML-escaped apostrophe, so check for the surrounding wording
        assert "Link unavailable" in r.text
        assert "new WhatsApp message" in r.text


class TestPostForm:
    async def test_happy_path(self, client_and_uc):
        client, uc, users, drafts = client_and_uc
        await drafts.save(_ready_draft())
        url = await uc.issue_link("+34600000001")
        token = url.split("token=")[1]

        r = client.post(
            "/setup/credentials",
            data={"token": token, "nie": "X12345A", "password": "hunter2"},
        )
        assert r.status_code == 200
        assert "You're connected" in r.text
        assert "+34600000001" in users.users
        assert users.users["+34600000001"].insurer_credentials.password == "hunter2"

    async def test_replay_rejected(self, client_and_uc):
        client, uc, _, drafts = client_and_uc
        await drafts.save(_ready_draft())
        url = await uc.issue_link("+34600000001")
        token = url.split("token=")[1]
        client.post(
            "/setup/credentials",
            data={"token": token, "nie": "X12345A", "password": "hunter2"},
        )
        replay = client.post(
            "/setup/credentials",
            data={"token": token, "nie": "X12345A", "password": "hunter2"},
        )
        assert replay.status_code == 400
        assert "already been used" in replay.text

    async def test_bad_input(self, client_and_uc):
        client, uc, _, drafts = client_and_uc
        await drafts.save(_ready_draft())
        url = await uc.issue_link("+34600000001")
        token = url.split("token=")[1]
        r = client.post(
            "/setup/credentials",
            data={"token": token, "nie": "", "password": "hunter2"},
        )
        assert r.status_code == 400
        assert "required" in r.text.lower()
