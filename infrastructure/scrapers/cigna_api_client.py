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


_HOME_URL = "https://clientes.cigna.es/cp/api/private/home"
_CHIPCARD_URL = "https://clientes.cigna.es/cp/api/private/chipcard"
_AUTH_TOKENS_URL = "https://clientes.cigna.es/cp/api/public/authentication/tokens"
_SESSION_TOKEN_URL = "https://directorio-medico.cigna.es/dm/api/tuotempo/session-id-token"
_POLICIES_URL = "https://directorio-medico.cigna.es/dm/api/policies"
_SEARCH_URL = "https://directorio-medico.cigna.es/dm/api/providers/advanced-search"

# Cigna's private API endpoints redirect to /cp/home when called without
# the headers their SPA normally sends. The session cookies alone aren't
# enough — the server checks Referer + X-Requested-With to confirm the
# request is from inside the portal app.
_HEADERS_CLIENTES = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://clientes.cigna.es/cp/home",
    "X-Requested-With": "XMLHttpRequest",
}
_HEADERS_DIRECTORIO = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://directorio-medico.cigna.es/dm/cuadro-medico",
    "X-Requested-With": "XMLHttpRequest",
}


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

    async def fetch_home(self) -> str:
        """GET /cp/api/private/home.

        Returns the user's true `insuranceNumber` (e.g. "Z3512875K01" —
        full NIE + person-number suffix). This is NOT the same value the
        user types into the login form: Cigna accepts NIE/NIF/Pasaporte
        as login identifiers, but every downstream private API endpoint
        (/chipcard, /list, /messageForUser, …) expects the user's actual
        insurance number from /home. Always call this first.
        """
        response = await self._get(_HOME_URL, headers=_HEADERS_CLIENTES)
        body = await self._json(response, _HOME_URL)
        try:
            return body["insuranceNumber"]
        except (KeyError, TypeError) as e:
            raise CignaApiBadPayload(
                f"{_HOME_URL} response missing insuranceNumber: {e}"
            ) from e

    async def fetch_chipcard(self, insurance_number: str) -> ChipcardResponse:
        """GET /cp/api/private/chipcard.

        Takes the full `insuranceNumber` returned by /home (e.g. "Z3512875K01")
        — already has the person-number suffix embedded.
        """
        params = {"insuranceNumber": insurance_number}
        response = await self._get(_CHIPCARD_URL, params=params, headers=_HEADERS_CLIENTES)
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
        response = await self._get(_SESSION_TOKEN_URL, params=params, headers=_HEADERS_DIRECTORIO)
        body = await self._json(response, _SESSION_TOKEN_URL)

        # Cigna's response shape has shifted over time. Order is in
        # rough preference (newest format first based on observed responses).
        # Bare-value path remains for older clones.
        if isinstance(body, (int, str)):
            return str(body)
        if isinstance(body, dict):
            for key in ("ots_token", "sessionId", "session-id-token", "token"):
                if key in body:
                    return str(body[key])
        raise CignaApiBadPayload(
            f"{_SESSION_TOKEN_URL} returned unexpected shape "
            f"({type(body).__name__}); body={body!r}"
        )

    async def fetch_id_member(self, session_token: str) -> int:  # noqa: ARG002 — kept for sig compat
        """Get the user's `idMember` from the Cigna-issued JWT in cookies.

        The original chain used GET /dm/api/policies/<session-token>, but
        that endpoint now returns 400 with an HTML error page. The same
        `idMember` value is exposed as the `memberid` claim inside the
        Cigna JWT — and the JWT itself is already set as a cookie by
        Cigna's SPA right after login (cookie name varies, but the value
        always starts with "eyJ" and has 3 dot-separated segments).

        Reading the cookie is more robust than re-calling the JWT-issuing
        endpoint: the cookie keeps working even when Cigna renames or
        adds auth requirements to that endpoint (as they recently did —
        a fresh POST to /cp/api/public/authentication/tokens now 400s).
        """
        import base64
        import json

        cookies = await self._context.cookies()
        candidate_jwts = []
        for c in cookies:
            value = c.get("value", "")
            # JWT: 3 base64url segments separated by dots, header starts "eyJ".
            if value.startswith("eyJ") and value.count(".") == 2:
                candidate_jwts.append((c.get("name", "?"), value))

        for name, jwt in candidate_jwts:
            try:
                payload_b64 = jwt.split(".")[1]
                padding = "=" * (-len(payload_b64) % 4)
                payload = json.loads(
                    base64.urlsafe_b64decode(payload_b64 + padding)
                )
                if "memberid" in payload:
                    logger.info(
                        "Extracted idMember=%s from cookie %s",
                        payload["memberid"], name,
                    )
                    return int(payload["memberid"])
            except (ValueError, KeyError, TypeError):
                continue  # not the right JWT — try the next one

        raise CignaApiBadPayload(
            f"No Cigna JWT cookie with a `memberid` claim found "
            f"(found {len(candidate_jwts)} JWT-shaped cookies but none had memberid)"
        )

    # ---- composite ----

    async def fetch_user_context(self, nie: str) -> UserContext:  # noqa: ARG002 — nie kept for caller compat
        """Run the first four calls in order to gather everything needed for search.

        home → chipcard → session_token → idMember. Returns a `UserContext`.

        Why /home first: the login identifier (NIE/NIF/Pasaporte) is NOT
        the value the private API expects as `insuranceNumber`. For users
        who logged in with a passport, the two are completely different
        (e.g. login "YB1133771" → real insuranceNumber "Z3512875K01").
        /home returns the canonical `insuranceNumber`.
        """
        insurance_number = await self.fetch_home()
        chipcard_response = await self.fetch_chipcard(insurance_number)
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
        response = await self._get(_SEARCH_URL, params=params, headers=_HEADERS_DIRECTORIO)
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

    async def _get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> APIResponse:
        response = await self._context.request.get(
            url, params=params, headers=headers or {"Accept": "application/json"}
        )
        if not response.ok:
            text = await response.text()
            raise CignaApiBadStatus(url, response.status, text)
        return response

    async def _json(self, response: APIResponse, url: str) -> dict | list | int | str:
        try:
            return await response.json()
        except Exception as e:
            # Most common cause: forgot the Accept header and got XML back,
            # or the session is stale and we got redirected to a login page.
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
