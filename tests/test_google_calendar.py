"""Tests for GoogleCalendarService and OAuth endpoints."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.entities.appointment import Appointment, AppointmentStatus
from domain.entities.doctor import Doctor
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty
from domain.value_objects.time_slot import TimeSlot
from infrastructure.calendar.google_calendar_service import GoogleCalendarService


@pytest.fixture
def calendar_service():
    return GoogleCalendarService(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/auth/google/callback",
    )


@pytest.fixture
def user_with_token():
    return User(
        phone="+34612345678",
        name="Test User",
        home_address=Address(raw="Calle Test 1", city="Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer.CIGNA, username="u", password="p"
        ),
        google_calendar_token="fake-refresh-token",
    )


@pytest.fixture
def user_without_token():
    return User(
        phone="+34612345678",
        name="Test User",
        home_address=Address(raw="Calle Test 1", city="Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer.CIGNA, username="u", password="p"
        ),
        google_calendar_token=None,
    )


def _dummy_doctor():
    return Doctor(
        clinic_id="123",
        practitioner_id="456",
        name="Dr. Test",
        specialty=Specialty(name="PSICOLOGIA", type="E"),
        clinic_name="Test Clinic",
        phone="+34915631554",
        address=Address(raw="Calle Mayor 1, Madrid", city="Madrid"),
    )


class TestBuildAuthUrl:
    def test_generates_google_oauth_url(self, calendar_service):
        url = calendar_service.build_auth_url(state="+34612345678")
        assert "accounts.google.com" in url
        assert "client_id=test-client-id" in url
        assert "redirect_uri=http" in url
        assert "state=%2B34612345678" in url
        assert "access_type=offline" in url

    def test_includes_calendar_scope(self, calendar_service):
        url = calendar_service.build_auth_url()
        assert "calendar" in url


class TestGetBusyIntervals:
    async def test_returns_empty_without_token(self, calendar_service, user_without_token):
        result = await calendar_service.get_busy_intervals(user_without_token, lookahead_weeks=2)
        assert result == []

    async def test_returns_intervals_on_success(self, calendar_service, user_with_token):
        mock_freebusy_resp = {
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-05-25T09:00:00Z", "end": "2026-05-25T10:00:00Z"},
                        {"start": "2026-05-26T14:00:00Z", "end": "2026-05-26T15:00:00Z"},
                    ]
                }
            }
        }

        with patch.object(
            calendar_service, "_get_access_token", new_callable=AsyncMock
        ) as mock_token:
            mock_token.return_value = "fake-access-token"

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = mock_freebusy_resp

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

                result = await calendar_service.get_busy_intervals(user_with_token, lookahead_weeks=2)

        assert len(result) == 2
        assert result[0][0].hour == 9
        assert result[1][0].hour == 14

    async def test_returns_empty_on_token_refresh_failure(self, calendar_service, user_with_token):
        import httpx

        with patch.object(
            calendar_service, "_get_access_token", new_callable=AsyncMock
        ) as mock_token:
            mock_token.side_effect = httpx.HTTPStatusError(
                "Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
            )
            result = await calendar_service.get_busy_intervals(user_with_token, lookahead_weeks=2)
            assert result == []


class TestAddEvent:
    async def test_returns_none_without_token(self, calendar_service, user_without_token):
        appt = Appointment(
            id="test-id",
            user_phone="+34612345678",
            doctor=_dummy_doctor(),
            slot=TimeSlot(start=datetime(2026, 5, 25, 10, 0)),
            status=AppointmentStatus.CONFIRMED,
        )
        result = await calendar_service.add_event(user_without_token, appt)
        assert result is None

    async def test_creates_event_on_success(self, calendar_service, user_with_token):
        appt = Appointment(
            id="test-id",
            user_phone="+34612345678",
            doctor=_dummy_doctor(),
            slot=TimeSlot(start=datetime(2026, 5, 25, 10, 0)),
            status=AppointmentStatus.CONFIRMED,
        )

        with patch.object(
            calendar_service, "_get_access_token", new_callable=AsyncMock
        ) as mock_token:
            mock_token.return_value = "fake-access-token"

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"id": "event123"}

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

                result = await calendar_service.add_event(user_with_token, appt)

        assert result == "event123"

    async def test_returns_none_on_api_failure(self, calendar_service, user_with_token):
        appt = Appointment(
            id="test-id",
            user_phone="+34612345678",
            doctor=_dummy_doctor(),
            slot=TimeSlot(start=datetime(2026, 5, 25, 10, 0)),
            status=AppointmentStatus.CONFIRMED,
        )

        with patch.object(
            calendar_service, "_get_access_token", new_callable=AsyncMock
        ) as mock_token:
            mock_token.return_value = "fake-access-token"

            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_resp.text = "Forbidden"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

                result = await calendar_service.add_event(user_with_token, appt)

        assert result is None
