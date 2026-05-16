"""OnboardingStateMachine: walks a new user through the WhatsApp portion."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from application.onboarding import (
    CredentialsLinkIssuer,
    OnboardingDraft,
    OnboardingDraftRepository,
    OnboardingState,
    OnboardingStateMachine,
)
from application.ports import BaseMessagingClient
from domain.entities.appointment import Appointment
from domain.entities.user import User
from domain.value_objects.time_slot import TimePreference


# ---- fakes ----

class FakeWhatsApp(BaseMessagingClient):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to_phone: str, body: str) -> None:
        self.sent.append((to_phone, body))

    async def send_confirmation(self, user: User, appointment: Appointment) -> None:
        self.sent.append((user.phone, "[confirmation]"))

    async def request_otp(self, user: User) -> None:
        self.sent.append((user.phone, "[otp]"))

    @property
    def last_body(self) -> str:
        return self.sent[-1][1]


@dataclass
class InMemoryDraftRepo(OnboardingDraftRepository):
    drafts: dict[str, OnboardingDraft] = field(default_factory=dict)

    async def get_by_phone(self, phone: str) -> OnboardingDraft | None:
        return self.drafts.get(phone)

    async def save(self, draft: OnboardingDraft) -> None:
        self.drafts[draft.phone] = draft

    async def delete(self, phone: str) -> None:
        self.drafts.pop(phone, None)


class FakeLinkIssuer(CredentialsLinkIssuer):
    def __init__(self):
        self.issued: list[str] = []

    async def issue_link(self, phone: str) -> str:
        url = f"https://example.test/setup/credentials?token=tok-{len(self.issued)}"
        self.issued.append(phone)
        return url


# ---- fixtures ----

@pytest.fixture
def fsm():
    whatsapp = FakeWhatsApp()
    drafts = InMemoryDraftRepo()
    links = FakeLinkIssuer()
    machine = OnboardingStateMachine(
        draft_repo=drafts, whatsapp=whatsapp, credentials_link=links
    )
    return machine, whatsapp, drafts, links


# ---- happy path through the full flow ----

class TestHappyPath:
    async def test_first_message_starts_flow(self, fsm):
        machine, wa, drafts, _ = fsm
        await machine.handle_message("+34600000001", "hi")
        draft = drafts.drafts["+34600000001"]
        assert draft.state == OnboardingState.AWAITING_NAME
        assert "full name" in wa.last_body.lower()

    async def test_full_walkthrough_ends_with_magic_link(self, fsm):
        machine, wa, drafts, links = fsm
        phone = "+34600000001"

        await machine.handle_message(phone, "hi")  # → ask name
        await machine.handle_message(phone, "Antonella Masini")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_ADDRESS

        await machine.handle_message(phone, "Calle Donoso 20, Madrid")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_INSURER

        await machine.handle_message(phone, "Cigna")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_PREFERRED_TIME

        await machine.handle_message(phone, "afternoons")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_CREDENTIALS
        assert drafts.drafts[phone].preferred_times == TimePreference.AFTERNOONS

        # Last message should be the magic link.
        assert links.issued == [phone]
        assert "setup/credentials?token=" in wa.last_body
        assert "10 minutes" in wa.last_body


# ---- per-step retry behaviour ----

class TestRetries:
    async def test_short_name_re_prompts(self, fsm):
        machine, wa, drafts, _ = fsm
        phone = "+34600000001"
        await machine.handle_message(phone, "hi")  # asks name
        await machine.handle_message(phone, "a")  # too short
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_NAME
        assert "didn't catch" in wa.last_body.lower() or "full name" in wa.last_body.lower()

    async def test_bad_address_re_prompts(self, fsm):
        machine, _, drafts, _ = fsm
        phone = "+34600000001"
        await machine.handle_message(phone, "hi")
        await machine.handle_message(phone, "Antonella")
        await machine.handle_message(phone, "abc")  # too short
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_ADDRESS

    async def test_unsupported_insurer_re_prompts(self, fsm):
        machine, wa, drafts, _ = fsm
        phone = "+34600000001"
        for body in ("hi", "Antonella Masini", "Calle Donoso 20, Madrid"):
            await machine.handle_message(phone, body)
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_INSURER

        await machine.handle_message(phone, "adeslas")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_INSURER
        assert "cigna" in wa.last_body.lower()

    async def test_unknown_preferred_time_re_prompts(self, fsm):
        machine, _, drafts, _ = fsm
        phone = "+34600000001"
        for body in ("hi", "Antonella Masini", "Calle Donoso 20, Madrid", "Cigna"):
            await machine.handle_message(phone, body)
        await machine.handle_message(phone, "whenever I feel like it")
        assert drafts.drafts[phone].state == OnboardingState.AWAITING_PREFERRED_TIME


# ---- magic-link re-issue when user pings while awaiting credentials ----

class TestAwaitingCredentialsBehavior:
    async def test_message_in_credentials_state_re_issues_link(self, fsm):
        machine, wa, drafts, links = fsm
        phone = "+34600000001"
        for body in (
            "hi",
            "Antonella Masini",
            "Calle Donoso 20, Madrid",
            "Cigna",
            "afternoons",
        ):
            await machine.handle_message(phone, body)
        assert len(links.issued) == 1

        # User comes back later, sends anything
        await machine.handle_message(phone, "still need help?")
        assert len(links.issued) == 2  # link re-issued
        assert "setup/credentials" in wa.last_body
