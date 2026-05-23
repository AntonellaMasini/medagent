"""CignaApiClient tests — mock `BrowserContext.request.get` so we don't
spin up Playwright or talk to the live Cigna API.

The fixtures are pared-down versions of real Cigna responses captured from
authenticated DevTools sessions, May 2026. The endpoint chain has drifted
since then; see CLAUDE.md's "Cigna's discovered endpoint chain" section
for the current shape (last re-verified 2026-05-23).
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects.specialty import get_catalog
from infrastructure.scrapers.cigna_api_client import (
    CignaApiBadPayload,
    CignaApiBadStatus,
    CignaApiClient,
)

SPECIALTY = get_catalog().find_by_name("PSICOLOGIA")
assert SPECIALTY is not None


# ---- mocking helpers ----

def _make_response(*, ok=True, status=200, json_body=None, text_body=""):
    """Build a Playwright-APIResponse-shaped mock."""
    response = MagicMock()
    response.ok = ok
    response.status = status
    response.json = AsyncMock(return_value=json_body)
    response.text = AsyncMock(return_value=text_body)
    return response


def _make_context(url_to_response: dict, cookies: list | None = None):
    """Build a BrowserContext-shaped mock.

    `url_to_response` maps a URL *substring* to the response to return.
    The first matching key wins. AssertionError on unmatched URLs so a
    typo in a test fails loudly.

    `cookies` is a list of cookie dicts (Playwright shape: at minimum
    `name` and `value` keys) returned from `context.cookies()`.
    """
    context = MagicMock()
    captured_calls = []

    async def mock_get(url, *, params=None, headers=None):
        captured_calls.append({"url": url, "params": params, "headers": headers})
        for substring, response in url_to_response.items():
            if substring in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")

    context.request = MagicMock()
    context.request.get = mock_get
    context.cookies = AsyncMock(return_value=cookies or [])
    context.captured_calls = captured_calls  # exposed for assertions
    return context


def _make_jwt(payload: dict) -> str:
    """Build a JWT-shaped string. Signature isn't verified by the client,
    so we don't need to sign it — just produce three base64url segments."""
    header_b64 = "eyJhbGciOiJIUzI1NiJ9"  # {"alg":"HS256"}
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header_b64}.{payload_b64}.unused-signature"


# ---- fetch_home ----

HOME_OK_BODY = {
    "fullName": "MASINI ORTIZ, ANTONELLA",
    "insuranceNumber": "Z3512875K01",
    "isAuthorized": True,
}


class TestFetchHome:
    async def test_returns_insurance_number(self):
        context = _make_context({"/home": _make_response(json_body=HOME_OK_BODY)})
        client = CignaApiClient(context)
        assert await client.fetch_home() == "Z3512875K01"

    async def test_missing_field_raises_bad_payload(self):
        context = _make_context(
            {"/home": _make_response(json_body={"fullName": "X"})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_home()


# ---- fetch_chipcard ----

CHIPCARD_OK_BODY = {
    "functionalExceptions": [],
    "systemExceptions": [],
    "chipcards": [
        {
            "subscriberNumber": "Z3512875K",
            "personNumber": "01",
            "cardNumber": "010346461",
            "cardVersion": "0",
            "policy": {
                "code": "800251",
                "name": "FEVER",
                "planCode": "C67",
                "planDesc": "Cigna Salud cuadro Médico",
                "expired": False,
            },
            "startDate": "2025-12-01",
        }
    ],
}


class TestFetchChipcard:
    async def test_happy_path_extracts_chipcard_groupcode_subscriber(self):
        context = _make_context({"chipcard": _make_response(json_body=CHIPCARD_OK_BODY)})
        client = CignaApiClient(context)

        result = await client.fetch_chipcard("Z3512875K01")

        assert result.chipcard == "010346461"
        assert result.group_code == "800251"
        assert result.subscriber_number == "Z3512875K"

    async def test_passes_insurance_number_verbatim(self):
        """Caller provides the full insuranceNumber (with the `01` suffix
        already embedded — comes from /home). The client must NOT modify it,
        otherwise we'd double-append for users who logged in with a passport
        rather than an NIE.
        """
        context = _make_context({"chipcard": _make_response(json_body=CHIPCARD_OK_BODY)})
        client = CignaApiClient(context)
        await client.fetch_chipcard("Z3512875K01")

        sent_params = context.captured_calls[0]["params"]
        assert sent_params["insuranceNumber"] == "Z3512875K01"

    async def test_request_sends_spa_headers(self):
        """Cigna's private API endpoints redirect to /cp/home when the
        SPA headers are missing (Referer + X-Requested-With). Verify we
        send them along with an Accept that tolerates the wildcard fallback."""
        context = _make_context({"chipcard": _make_response(json_body=CHIPCARD_OK_BODY)})
        client = CignaApiClient(context)
        await client.fetch_chipcard("Z3512875K01")

        sent_headers = context.captured_calls[0]["headers"]
        assert "application/json" in sent_headers["Accept"]
        assert sent_headers["X-Requested-With"] == "XMLHttpRequest"
        assert "cigna.es" in sent_headers["Referer"]

    async def test_4xx_raises_bad_status(self):
        context = _make_context(
            {
                "chipcard": _make_response(
                    ok=False, status=403, text_body="Forbidden"
                )
            }
        )
        client = CignaApiClient(context)

        with pytest.raises(CignaApiBadStatus) as exc:
            await client.fetch_chipcard("Z3512875K01")
        assert exc.value.status == 403

    async def test_empty_chipcards_array_raises_bad_payload(self):
        context = _make_context(
            {"chipcard": _make_response(json_body={"chipcards": []})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_chipcard("Z3512875K01")

    async def test_missing_card_number_raises_bad_payload(self):
        bad = {
            "chipcards": [
                {
                    "subscriberNumber": "Z3512875K",
                    "policy": {"code": "800251"},
                    # cardNumber missing
                }
            ]
        }
        context = _make_context({"chipcard": _make_response(json_body=bad)})
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_chipcard("Z3512875K01")


# ---- fetch_session_token ----

class TestFetchSessionToken:
    async def test_bare_string_token(self):
        context = _make_context(
            {"session-id-token": _make_response(json_body="2083728659")}
        )
        client = CignaApiClient(context)
        assert await client.fetch_session_token("010346461") == "2083728659"

    async def test_bare_integer_token(self):
        """Cigna's token is a Java signed int — could come back as int or string."""
        context = _make_context(
            {"session-id-token": _make_response(json_body=-1784192811)}
        )
        client = CignaApiClient(context)
        assert await client.fetch_session_token("010346461") == "-1784192811"

    async def test_dict_with_sessionid_key(self):
        context = _make_context(
            {
                "session-id-token": _make_response(
                    json_body={"sessionId": "abc123"}
                )
            }
        )
        client = CignaApiClient(context)
        assert await client.fetch_session_token("010346461") == "abc123"

    async def test_dict_with_ots_token_key(self):
        """Current production shape (verified 2026-05-23) returns
        {"executionTime":..., "message":..., "result":"OK", "ots_token":...}.
        The old `sessionId` / `session-id-token` / `token` keys are kept
        as fallbacks for older clones.
        """
        context = _make_context(
            {
                "session-id-token": _make_response(
                    json_body={
                        "executionTime": "263",
                        "message": "User N25M13270055 updated",
                        "result": "OK",
                        "ots_token": "zoor92dpptafn",
                    }
                )
            }
        )
        client = CignaApiClient(context)
        assert await client.fetch_session_token("010346461") == "zoor92dpptafn"

    async def test_unexpected_shape_raises(self):
        context = _make_context(
            {"session-id-token": _make_response(json_body={"unrelated": "field"})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_session_token("010346461")


# ---- fetch_id_member ----
#
# The original chain read idMember from GET /dm/api/policies/<session-token>,
# but that endpoint now returns 400 + HTML. The same value is exposed as the
# `memberid` claim inside a JWT cookie that Cigna's SPA sets after login.
# These tests cover the cookie-decoding path.


class TestFetchIdMember:
    async def test_happy_path_decodes_memberid_from_jwt_cookie(self):
        jwt = _make_jwt({"sub": "YB1133771", "memberid": "948849"})
        context = _make_context(
            url_to_response={},  # no HTTP calls expected
            cookies=[{"name": "_uInWj_obfuscated_", "value": jwt}],
        )
        client = CignaApiClient(context)
        # The session_token argument is kept for signature compatibility
        # with the original API; the value is unused.
        assert await client.fetch_id_member("unused") == 948849

    async def test_ignores_non_jwt_cookies(self):
        """Most cookies are session/tracking junk. The JWT scan must pick
        the right one and ignore the rest."""
        jwt = _make_jwt({"sub": "YB1133771", "memberid": "948849"})
        context = _make_context(
            url_to_response={},
            cookies=[
                {"name": "JSESSIONID", "value": "DFD4C8F13B71C5414C2525"},
                {"name": "_vwo_uuid", "value": "D7B945C1A141"},
                {"name": "OptanonConsent", "value": "isGpcEnabled=0&..."},
                {"name": "the_jwt_one", "value": jwt},  # this one wins
            ],
        )
        client = CignaApiClient(context)
        assert await client.fetch_id_member("unused") == 948849

    async def test_skips_jwts_without_memberid_claim(self):
        """Cigna sets multiple JWT-shaped cookies; only one has the
        memberid claim. The others (e.g. Okta access tokens) must be
        passed over, not raised on."""
        wrong_jwt = _make_jwt({"sub": "something", "scope": "openid"})
        right_jwt = _make_jwt({"sub": "YB1133771", "memberid": "948849"})
        context = _make_context(
            url_to_response={},
            cookies=[
                {"name": "access_token", "value": wrong_jwt},
                {"name": "cigna_jwt", "value": right_jwt},
            ],
        )
        client = CignaApiClient(context)
        assert await client.fetch_id_member("unused") == 948849

    async def test_no_jwt_with_memberid_raises_bad_payload(self):
        wrong_jwt = _make_jwt({"sub": "something", "scope": "openid"})
        context = _make_context(
            url_to_response={},
            cookies=[
                {"name": "JSESSIONID", "value": "junk"},
                {"name": "access_token", "value": wrong_jwt},
            ],
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_id_member("unused")


# ---- search_doctors ----

SEARCH_OK_BODY = {
    "content": [
        {
            "id": "-1402186762",
            "name": "HERMOSO IZQUIERDO, SOLEDAD",
            "addresses": [
                {
                    "id": 1501103525,
                    "provider": "SH PSICOSALUD",
                    "address": "C. Don Pedro, 17",
                    "city": "MADRID",
                    "phone1": "915631554",
                    "geoPoint": {"lat": 40.411784, "lon": -3.714223},
                    "postcd": "28005",
                    "distanceToSearchPoint": 0.45,
                }
            ],
        }
    ]
}


class TestSearchDoctors:
    async def test_returns_content_array(self):
        context = _make_context(
            {"advanced-search": _make_response(json_body=SEARCH_OK_BODY)}
        )
        client = CignaApiClient(context)

        result = await client.search_doctors(
            specialty=SPECIALTY,
            chipcard="010346461",
            id_member=948849,
            group_code="800251",
        )

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "HERMOSO IZQUIERDO, SOLEDAD"

    async def test_sends_required_query_params(self):
        context = _make_context(
            {"advanced-search": _make_response(json_body=SEARCH_OK_BODY)}
        )
        client = CignaApiClient(context)
        await client.search_doctors(
            specialty=SPECIALTY,
            chipcard="010346461",
            id_member=948849,
            group_code="800251",
        )

        params = context.captured_calls[0]["params"]
        assert params["chipcard"] == "010346461"
        assert params["idMember"] == "948849"
        assert params["groupCode"] == "800251"
        assert params["specialtyMedicalActDescription"] == "PSICOLOGIA"
        assert params["type"] == "SPECIALTY"
        assert params["publishable"] == "true"
        assert params["outpatient"] == "N"

    async def test_empty_content_returns_empty_list(self):
        """No doctors found is not an error — it's a valid "nothing matched"."""
        context = _make_context(
            {"advanced-search": _make_response(json_body={"content": []})}
        )
        client = CignaApiClient(context)
        result = await client.search_doctors(
            specialty=SPECIALTY,
            chipcard="010346461",
            id_member=948849,
            group_code="800251",
        )
        assert result == []

    async def test_missing_content_key_returns_empty_list(self):
        context = _make_context(
            {"advanced-search": _make_response(json_body={"otherKey": []})}
        )
        client = CignaApiClient(context)
        result = await client.search_doctors(
            specialty=SPECIALTY,
            chipcard="010346461",
            id_member=948849,
            group_code="800251",
        )
        assert result == []

    async def test_content_wrong_type_raises(self):
        """Defensive: if Cigna ever sends `content: "something"`, fail loud."""
        context = _make_context(
            {"advanced-search": _make_response(json_body={"content": "oops"})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.search_doctors(
                specialty=SPECIALTY,
                chipcard="010346461",
                id_member=948849,
                group_code="800251",
            )


# ---- fetch_user_context (composite) ----

class TestFetchUserContext:
    async def test_chains_calls_in_order(self):
        """Verify the composite makes home → chipcard → session-id-token,
        then decodes idMember from the JWT cookie. The `nie` arg is kept
        for caller compatibility but is unused — the insurance number is
        read from /home, not from the caller.
        """
        jwt = _make_jwt({"sub": "YB1133771", "memberid": "948849"})
        context = _make_context(
            url_to_response={
                "/home": _make_response(json_body=HOME_OK_BODY),
                "chipcard": _make_response(json_body=CHIPCARD_OK_BODY),
                "session-id-token": _make_response(
                    json_body={"result": "OK", "ots_token": "zoor92dpptafn"}
                ),
            },
            cookies=[{"name": "cigna_jwt", "value": jwt}],
        )
        client = CignaApiClient(context)

        user_ctx = await client.fetch_user_context("ignored-arg")

        assert user_ctx.chipcard == "010346461"
        assert user_ctx.group_code == "800251"
        assert user_ctx.id_member == 948849

        urls_called = [c["url"] for c in context.captured_calls]
        # /home must come first so we have the right insuranceNumber
        # before /chipcard, which depends on it.
        home_idx = next(i for i, u in enumerate(urls_called) if "/home" in u)
        chipcard_idx = next(
            i for i, u in enumerate(urls_called) if "chipcard" in u
        )
        session_idx = next(
            i for i, u in enumerate(urls_called) if "session-id-token" in u
        )
        assert home_idx < chipcard_idx < session_idx

    async def test_chipcard_receives_insurance_number_from_home(self):
        """/home returns the insuranceNumber; /chipcard must call with
        that exact value (not the caller's `nie` argument)."""
        jwt = _make_jwt({"sub": "YB1133771", "memberid": "948849"})
        context = _make_context(
            url_to_response={
                "/home": _make_response(json_body=HOME_OK_BODY),
                "chipcard": _make_response(json_body=CHIPCARD_OK_BODY),
                "session-id-token": _make_response(
                    json_body={"ots_token": "tkn"}
                ),
            },
            cookies=[{"name": "cigna_jwt", "value": jwt}],
        )
        client = CignaApiClient(context)
        await client.fetch_user_context("LOGIN-IDENTIFIER-IGNORED")

        chipcard_call = next(
            c for c in context.captured_calls if "chipcard" in c["url"]
        )
        # Value from HOME_OK_BODY, not the caller's argument.
        assert chipcard_call["params"]["insuranceNumber"] == "Z3512875K01"

    async def test_failure_in_home_short_circuits(self):
        context = _make_context(
            url_to_response={
                "/home": _make_response(ok=False, status=500, text_body="err"),
                "chipcard": _make_response(json_body="should-not-be-called"),
            },
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadStatus):
            await client.fetch_user_context("Z3512875K")
        urls_called = [c["url"] for c in context.captured_calls]
        assert all("chipcard" not in u for u in urls_called)

    async def test_failure_in_chipcard_short_circuits(self):
        context = _make_context(
            url_to_response={
                "/home": _make_response(json_body=HOME_OK_BODY),
                "chipcard": _make_response(ok=False, status=500, text_body="err"),
                "session-id-token": _make_response(json_body="should-not-be-called"),
            },
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadStatus):
            await client.fetch_user_context("Z3512875K")
        urls_called = [c["url"] for c in context.captured_calls]
        assert all("session-id-token" not in u for u in urls_called)
