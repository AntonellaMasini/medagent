"""Google Calendar integration via REST API (using httpx).

Handles OAuth2 token refresh and provides:
  - get_busy_intervals(): freebusy query for slot validation
  - add_event(): create a calendar event after booking
  - build_auth_url() / exchange_code(): OAuth2 authorization flow
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from application.ports import BaseCalendarService
from domain.entities.appointment import Appointment
from domain.entities.user import User

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

SCOPES = "https://www.googleapis.com/auth/calendar"


class GoogleCalendarService(BaseCalendarService):
    """Production implementation of calendar service using Google Calendar API."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def build_auth_url(self, state: str = "") -> str:
        """Generate the Google OAuth2 authorization URL."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for tokens.

        Returns dict with 'access_token', 'refresh_token', 'expires_in'.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self._redirect_uri,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_access_token(self, refresh_token: str) -> str:
        """Use refresh token to get a fresh access token."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def get_busy_intervals(
        self, user: User, lookahead_weeks: int
    ) -> list[tuple[datetime, datetime]]:
        """Query Google Calendar freebusy API for the user's busy times."""
        if not user.google_calendar_token:
            return []

        try:
            access_token = await self._get_access_token(user.google_calendar_token)
        except httpx.HTTPStatusError:
            logger.warning("Failed to refresh Google Calendar token for %s", user.phone)
            return []

        now = datetime.now(timezone.utc)
        time_max = now + timedelta(weeks=lookahead_weeks)

        body = {
            "timeMin": now.isoformat(),
            "timeMax": time_max.isoformat(),
            "items": [{"id": "primary"}],
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GOOGLE_CALENDAR_API}/freeBusy",
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Google Calendar freebusy query failed: %s", resp.text
                )
                return []

            data = resp.json()

        busy_list = data.get("calendars", {}).get("primary", {}).get("busy", [])
        intervals = []
        for entry in busy_list:
            start = datetime.fromisoformat(entry["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(entry["end"].replace("Z", "+00:00"))
            intervals.append((start, end))

        logger.info(
            "Google Calendar: %d busy intervals for %s (next %d weeks)",
            len(intervals),
            user.phone,
            lookahead_weeks,
        )
        return intervals

    async def add_event(self, user: User, appointment: Appointment) -> str | None:
        """Create a Google Calendar event for a confirmed appointment.

        Returns the event ID on success, None on failure.
        """
        if not user.google_calendar_token:
            logger.info("No Google Calendar token for %s, skipping event creation", user.phone)
            return None

        try:
            access_token = await self._get_access_token(user.google_calendar_token)
        except httpx.HTTPStatusError:
            logger.warning("Failed to refresh Google Calendar token for %s", user.phone)
            return None

        event_body = {
            "summary": f"Cita médica: {appointment.doctor.specialty.name.title()}",
            "description": (
                f"Doctor: {appointment.doctor.name}\n"
                f"Clínica: {appointment.doctor.clinic_name}\n"
                f"Teléfono: {appointment.doctor.phone}\n"
                f"Dirección: {appointment.doctor.address.raw}"
            ),
            "location": appointment.doctor.address.raw,
            "start": {
                "dateTime": appointment.slot.start.isoformat(),
                "timeZone": "Europe/Madrid",
            },
            "end": {
                "dateTime": appointment.slot.end.isoformat(),
                "timeZone": "Europe/Madrid",
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                    {"method": "popup", "minutes": 15},
                ],
            },
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                json=event_body,
            )
            if resp.status_code in (200, 201):
                event_id = resp.json().get("id", "")
                logger.info(
                    "Created Google Calendar event %s for %s",
                    event_id,
                    user.phone,
                )
                return event_id
            else:
                logger.warning(
                    "Failed to create calendar event: %s %s",
                    resp.status_code,
                    resp.text,
                )
                return None
