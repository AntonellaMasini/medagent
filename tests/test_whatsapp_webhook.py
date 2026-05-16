"""Routing tests for the WhatsApp inbound webhook.

The webhook has 3 routing rules with a strict precedence — these tests pin
the precedence down so a future refactor doesn't silently change it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.book_appointment import AppointmentRequest, BookAppointmentUseCase
from application.handle_otp import HandleOTPUseCase
from application.onboarding import OnboardingStateMachine
from application.ports import (
    BaseInsurerScraper,
    BaseMessagingClient,
    BaseOTPRelay,
    BaseVoiceCaller,
)
from domain.entities.appointment import Appointment
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.repositories.appointment_repository import AppointmentRepository
from domain.repositories.user_repository import UserRepository
from domain.value_objects.address import Address
from interfaces.webhooks.whatsapp_webhook import build_whatsapp_router


# ---- fakes ----

class FakeWhatsApp(BaseMessagingClient):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to_phone: str, body: str) -> None:
        self.sent.append((to_phone, body))

    async def send_confirmation(self, user, appointment) -> None:  # noqa: ARG002
        pass

    async def request_otp(self, user) -> None:  # noqa: ARG002
        pass


class FakeOTPRelay(BaseOTPRelay):
    def __init__(self, *, waiting: bool = False):
        self._waiting = waiting
        self.submitted: list[tuple[str, str]] = []

    async def wait_for_code(self, user_phone: str, timeout_seconds: int) -> str | None:
        return None

    async def submit_code(self, user_phone: str, code: str) -> None:
        self.submitted.append((user_phone, code))

    def is_waiting(self, user_phone: str) -> bool:
        return self._waiting


@dataclass
class FakeUserRepo(UserRepository):
    users: dict[str, User] = field(default_factory=dict)

    async def get_by_phone(self, phone: str) -> User | None:
        return self.users.get(phone)

    async def save(self, user: User) -> None:
        self.users[user.phone] = user

    async def delete(self, phone: str) -> None:
        self.users.pop(phone, None)


class FakeStateMachine(OnboardingStateMachine):
    """OnboardingStateMachine with no real collaborators; just records calls."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def handle_message(self, phone: str, body: str) -> None:
        self.calls.append((phone, body))


class RecordingBookUseCase(BookAppointmentUseCase):
    def __init__(self):
        self.calls: list[tuple[User, AppointmentRequest]] = []

    async def execute(self, user: User, request: AppointmentRequest):
        self.calls.append((user, request))
        return None


class FakeAppointmentRepo(AppointmentRepository):
    async def save(self, appointment: Appointment) -> None:
        pass

    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        return None

    async def list_for_user(self, user_phone: str) -> list[Appointment]:
        return []


class _NeverScraper(BaseInsurerScraper):
    async def find_doctors(self, **kwargs):  # noqa: ARG002
        raise AssertionError("scraper must not be invoked from these tests")


class _NeverVoice(BaseVoiceCaller):
    async def book_first_available(self, **kwargs):  # noqa: ARG002
        raise AssertionError("voice must not be invoked from these tests")


# ---- helpers ----

def _make_app(*, waiting: bool, registered: bool):
    whatsapp = FakeWhatsApp()
    otp_relay = FakeOTPRelay(waiting=waiting)
    user_repo = FakeUserRepo()
    if registered:
        user_repo.users["+34600000001"] = User(
            phone="+34600000001",
            name="Registered User",
            home_address=Address(raw="Calle Test 1, Madrid"),
            insurer_credentials=InsurerCredentials(
                insurer=Insurer.CIGNA, username="X1", password="pw"
            ),
        )
    onboarding = FakeStateMachine()
    book_use_case = RecordingBookUseCase()
    handle_otp = HandleOTPUseCase(otp_relay=otp_relay)

    app = FastAPI()
    app.include_router(
        build_whatsapp_router(
            user_repo=user_repo,
            book_use_case=book_use_case,
            onboarding=onboarding,
            handle_otp=handle_otp,
            otp_relay=otp_relay,
            whatsapp=whatsapp,
        )
    )
    return (
        TestClient(app),
        whatsapp,
        otp_relay,
        user_repo,
        onboarding,
        book_use_case,
    )


def _post(client: TestClient, body: str, phone: str = "+34600000001"):
    return client.post(
        "/webhooks/whatsapp",
        data={"From": f"whatsapp:{phone}", "Body": body},
    )


# ---- the routing matrix ----

class TestOTPWaitActive:
    """is_waiting=True branch — covers the bot-flagged regression."""

    def test_digits_in_body_submit_to_relay(self):
        client, wa, relay, _, onboarding, book = _make_app(
            waiting=True, registered=True
        )
        r = _post(client, "123456")
        assert r.status_code == 200
        assert relay.submitted == [("+34600000001", "123456")]
        assert onboarding.calls == []
        assert book.calls == []
        # No nudge — the relay was satisfied.
        assert not any("Still waiting" in b for _, b in wa.sent)

    def test_no_digits_sends_nudge_and_does_not_fall_through(self):
        """The regression: previously, a non-digit message was silently dropped.

        Also pins down that we don't fall through to booking/onboarding,
        which would spawn a concurrent scraper session.
        """
        client, wa, relay, _, onboarding, book = _make_app(
            waiting=True, registered=True
        )
        r = _post(client, "I haven't received the code")
        assert r.status_code == 200
        assert relay.submitted == []  # nothing submitted
        assert onboarding.calls == []  # no onboarding kicked off
        assert book.calls == []  # no concurrent scraper session
        # The user got a nudge instead of silence.
        assert any("Still waiting" in b for _, b in wa.sent)

    def test_specialty_in_body_does_not_spawn_concurrent_scraper(self):
        """Critical: while OTP is pending, a specialty word must NOT trigger
        a second booking flow."""
        client, _, _, _, _, book = _make_app(waiting=True, registered=True)
        r = _post(client, "actually find me a dermatologist instead")
        assert r.status_code == 200
        assert book.calls == []


class TestNoOTPWait:
    """is_waiting=False branch — normal routing."""

    def test_registered_user_with_specialty_routes_to_booking(self):
        client, _, _, _, onboarding, book = _make_app(
            waiting=False, registered=True
        )
        r = _post(client, "book me a psychologist")
        assert r.status_code == 200
        assert len(book.calls) == 1
        user, req = book.calls[0]
        assert user.phone == "+34600000001"
        assert req.specialty.value == "PSICOLOGIA"
        assert onboarding.calls == []

    def test_registered_user_no_specialty_gets_hint(self):
        client, wa, _, _, _, book = _make_app(waiting=False, registered=True)
        r = _post(client, "hello there")
        assert r.status_code == 200
        assert book.calls == []
        assert any("specialty" in b.lower() for _, b in wa.sent)

    def test_new_user_routes_to_onboarding(self):
        client, _, _, _, onboarding, book = _make_app(
            waiting=False, registered=False
        )
        r = _post(client, "hi")
        assert r.status_code == 200
        assert onboarding.calls == [("+34600000001", "hi")]
        assert book.calls == []

    def test_stray_digits_do_not_get_consumed_as_otp(self):
        """The other direction of the regression: digit strings should reach
        onboarding/booking when no scraper is actually waiting."""
        client, _, relay, _, onboarding, _ = _make_app(
            waiting=False, registered=False
        )
        r = _post(client, "28015")  # looks like an OTP, but no waiter
        assert r.status_code == 200
        assert relay.submitted == []
        # routed to onboarding instead
        assert onboarding.calls == [("+34600000001", "28015")]


# ---- response shape ----

def test_response_is_twiml_xml():
    client, *_ = _make_app(waiting=False, registered=False)
    r = _post(client, "hi")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Response>" in r.text


def test_background_tasks_run_synchronously_via_testclient():
    """TestClient runs background tasks after returning the response. Sanity
    check that the nudge actually fires before we look at FakeWhatsApp."""
    client, wa, *_ = _make_app(waiting=True, registered=True)
    _post(client, "what?")
    # If the background task didn't fire, this would be empty.
    assert any("Still waiting" in b for _, b in wa.sent)


