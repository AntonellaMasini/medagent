"""Cigna's authenticated JSON API client.

Reverse-engineered May 2026 from authenticated DevTools sessions. The
endpoint chain (in order) is:

  GET /cp/api/private/chipcard?insuranceNumber=<NIE>01
      → chipcard, groupCode

  GET /dm/api/tuotempo/session-id-token?chipcard=…&authorized=true
      → rotating session token

  GET /dm/api/policies/<session-token>
      → idMember

  GET /dm/api/providers/advanced-search?chipcard=…&idMember=…&groupCode=…&…
      → list of doctors

See `CLAUDE.md` for the architectural rule this exemplifies: browser handles
auth (Playwright login + OTP), API handles data. All HTTP goes through the
already-authenticated `BrowserContext.request` so session cookies travel
automatically.

Always sends `Accept: application/json` — without it, some Cigna endpoints
content-negotiate to XML.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.async_api import APIResponse, BrowserContext

from domain.value_objects.specialty import Specialty

logger = logging.getLogger(__name__)


_CHIPCARD_URL = "https://clientes.cigna.es/cp/api/private/chipcard"
_SESSION_TOKEN_URL = "https://directorio-medico.cigna.es/dm/api/tuotempo/session-id-token"
_POLICIES_URL = "https://directorio-medico.cigna.es/dm/api/policies"
_SEARCH_URL = "https://directorio-medico.cigna.es/dm/api/providers/advanced-search"

_JSON_HEADERS = {"Accept": "application/json"}


# ---- Response value objects ----

@dataclass(frozen=True)
class ChipcardResponse:
    """Parsed payload from /cp/api/private/chipcard."""

    chipcard: str
    group_code: str
    subscriber_number: str  # echoes the NIE without the "01" suffix


@dataclass(frozen=True)
class UserContext:
    """Everything needed to call the advanced-search endpoint for a user."""

    chipcard: str
    group_code: str
    id_member: int


# ---- Errors ----

class CignaApiError(Exception):
    """Base for any failure talking to Cigna's directorio-medico API."""


class CignaApiBadStatus(CignaApiError):
    def __init__(self, url: str, status: int, body_excerpt: str):
        self.url = url
        self.status = status
        self.body_excerpt = body_excerpt
        super().__init__(f"{url} returned HTTP {status}: {body_excerpt[:200]}")


class CignaApiBadPayload(CignaApiError):
    """Status was 2xx but the body didn't have the field we expected."""


# ---- Client ----

class CignaApiClient:
    """Thin HTTP wrapper around Cigna's directorio-medico API.

    Constructor takes an authenticated `BrowserContext` (typically just after
    Playwright's login flow finishes). All requests inherit that context's
    cookies, so we never touch credentials here.
    """

    def __init__(self, context: BrowserContext):
        self._context = context

    # ---- single-endpoint methods ----

    async def fetch_chipcard(self, nie: str) -> ChipcardResponse:
        """GET /cp/api/private/chipcard.

        The `01` suffix on the insuranceNumber param is the person number
        within the policy. `01` = primary policyholder; family members
        would be `02`/`03`/...  MVP scope assumes primary-only.
        """
        params = {"insuranceNumber": f"{nie}01"}
        response = await self._get(_CHIPCARD_URL, params=params)
        body = await self._json(response, _CHIPCARD_URL)

        try:
            card = body["chipcards"][0]
            return ChipcardResponse(
                chipcard=card["cardNumber"],
                group_code=card["policy"]["code"],
                subscriber_number=card["subscriberNumber"],
            )
        except (KeyError, IndexError, TypeError) as e:
            raise CignaApiBadPayload(
                f"{_CHIPCARD_URL} response missing expected fields: {e}"
            ) from e

    async def fetch_session_token(self, chipcard: str) -> str:
        """GET /dm/api/tuotempo/session-id-token.

        Returns a rotating session token (Java signed int as string, e.g.
        "2083728659" or "-1784192811"). Don't persist this — it's bound to
        the current login session.
        """
        params = {"chipcard": chipcard, "authorized": "true"}
        response = await self._get(_SESSION_TOKEN_URL, params=params)
        body = await self._json(response, _SESSION_TOKEN_URL)

        # Cigna returns either a bare value or {"sessionId": ...}.
        # Tolerate both — verified shape may shift between releases.
        if isinstance(body, (int, str)):
            return str(body)
        if isinstance(body, dict):
            for key in ("sessionId", "session-id-token", "token"):
                if key in body:
                    return str(body[key])
        raise CignaApiBadPayload(
            f"{_SESSION_TOKEN_URL} returned unexpected shape: {type(body).__name__}"
        )

    async def fetch_id_member(self, session_token: str) -> int:
        """GET /dm/api/policies/<session-token>.

        Returns the user's `idMember` (a stable integer per user).
        """
        url = f"{_POLICIES_URL}/{session_token}"
        response = await self._get(url)
        body = await self._json(response, url)
        try:
            return int(body["idMember"])
        except (KeyError, TypeError, ValueError) as e:
            raise CignaApiBadPayload(
                f"{url} response missing/invalid idMember: {e}"
            ) from e

    # ---- composite ----

    async def fetch_user_context(self, nie: str) -> UserContext:
        """Run the first three calls in order to gather everything needed for search.

        chipcard → session_token → idMember. Returns a `UserContext` with
        the three values bundled.
        """
        chipcard_response = await self.fetch_chipcard(nie)
        session_token = await self.fetch_session_token(chipcard_response.chipcard)
        id_member = await self.fetch_id_member(session_token)
        logger.info(
            "Cigna user context resolved: idMember=%d (chipcard masked)", id_member
        )
        return UserContext(
            chipcard=chipcard_response.chipcard,
            group_code=chipcard_response.group_code,
            id_member=id_member,
        )

    # ---- search ----

    async def search_doctors(
        self,
        *,
        specialty: Specialty,
        chipcard: str,
        id_member: int,
        group_code: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """GET /dm/api/providers/advanced-search.

        Returns the raw `content[]` from the API response — a list of doctor
        dicts. Caller is responsible for parsing them into `Doctor`
        entities (see `CignaScraper._parse_doctors`).
        """
        params = {
            "chipcard": chipcard,
            "idMember": str(id_member),
            "groupCode": group_code,
            "specialtyMedicalActDescription": specialty.name,
            "type": specialty.type.value,
            "limit": str(limit),
            "offset": str(offset),
            "orderType": "DEFAULT",
            "directionSort": "DESC",
            "publishable": "true",
            "outpatient": "N",
            "searchByDistanceTypes": "EN",
        }
        response = await self._get(_SEARCH_URL, params=params)
        body = await self._json(response, _SEARCH_URL)
        content = body.get("content", []) if isinstance(body, dict) else []
        if not isinstance(content, list):
            raise CignaApiBadPayload(
                f"{_SEARCH_URL} content was {type(content).__name__}, expected list"
            )
        logger.info(
            "Cigna search returned %d doctors for %s",
            len(content),
            specialty.name,
        )
        return content

    # ---- internals ----

    async def _get(self, url: str, *, params: dict | None = None) -> APIResponse:
        response = await self._context.request.get(
            url, params=params, headers=_JSON_HEADERS
        )
        if not response.ok:
            text = await response.text()
            raise CignaApiBadStatus(url, response.status, text)
        return response

    async def _json(self, response: APIResponse, url: str) -> dict | list | int | str:
        try:
            return await response.json()
        except Exception as e:
            # Most common cause: forgot the Accept header and got XML back.
            text = await response.text()
            raise CignaApiBadPayload(
                f"{url} returned non-JSON body (got {text[:100]!r}): {e}"
            ) from e


__all__ = [
    "CignaApiBadPayload",
    "CignaApiBadStatus",
    "CignaApiClient",
    "CignaApiError",
    "ChipcardResponse",
    "UserContext",
]
