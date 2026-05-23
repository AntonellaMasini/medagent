"""CignaApiClient tests — mock `BrowserContext.request.get` so we don't
spin up Playwright or talk to the live Cigna API.

The fixtures are pared-down versions of real Cigna responses captured from
authenticated DevTools sessions, May 2026.
"""
from __future__ import annotations

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


def _make_context(url_to_response: dict):
    """Build a BrowserContext-shaped mock.

    `url_to_response` maps a URL *substring* to the response to return.
    The first matching key wins. AssertionError on unmatched URLs so a
    typo in a test fails loudly.
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
    context.captured_calls = captured_calls  # exposed for assertions
    return context


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

        result = await client.fetch_chipcard("Z3512875K")

        assert result.chipcard == "010346461"
        assert result.group_code == "800251"
        assert result.subscriber_number == "Z3512875K"

    async def test_request_appends_01_to_nie(self):
        """The `01` suffix on insuranceNumber is the primary policyholder."""
        context = _make_context({"chipcard": _make_response(json_body=CHIPCARD_OK_BODY)})
        client = CignaApiClient(context)
        await client.fetch_chipcard("Z3512875K")

        sent_params = context.captured_calls[0]["params"]
        assert sent_params["insuranceNumber"] == "Z3512875K01"

    async def test_request_sends_accept_json_header(self):
        context = _make_context({"chipcard": _make_response(json_body=CHIPCARD_OK_BODY)})
        client = CignaApiClient(context)
        await client.fetch_chipcard("Z3512875K")

        sent_headers = context.captured_calls[0]["headers"]
        assert sent_headers["Accept"] == "application/json"

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
            await client.fetch_chipcard("Z3512875K")
        assert exc.value.status == 403

    async def test_empty_chipcards_array_raises_bad_payload(self):
        context = _make_context(
            {"chipcard": _make_response(json_body={"chipcards": []})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_chipcard("Z3512875K")

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
            await client.fetch_chipcard("Z3512875K")


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

    async def test_unexpected_shape_raises(self):
        context = _make_context(
            {"session-id-token": _make_response(json_body={"unrelated": "field"})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_session_token("010346461")


# ---- fetch_id_member ----

POLICIES_OK_BODY = {
    "id": 1259851,
    "idMember": 948849,
    "fullName": "MASINI ORTIZ,ANTONELLA",
    "uniqueRegisterId": "N25M13270055",
    "policiesForUser": [
        {
            "id": 41448,
            "groupCode": "800251",
            "groupName": "FEVER",
        }
    ],
}


class TestFetchIdMember:
    async def test_happy_path(self):
        context = _make_context({"policies": _make_response(json_body=POLICIES_OK_BODY)})
        client = CignaApiClient(context)
        assert await client.fetch_id_member("2083728659") == 948849

    async def test_token_lands_in_url_path(self):
        context = _make_context({"policies": _make_response(json_body=POLICIES_OK_BODY)})
        client = CignaApiClient(context)
        await client.fetch_id_member("-1784192811")

        called_url = context.captured_calls[0]["url"]
        assert called_url.endswith("/policies/-1784192811")

    async def test_missing_id_member_raises(self):
        context = _make_context(
            {"policies": _make_response(json_body={"fullName": "X"})}
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadPayload):
            await client.fetch_id_member("sometoken")


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
    async def test_chains_three_calls(self):
        """Verify the composite makes all three calls in order, and passes
        the right output of each to the next."""
        context = _make_context(
            {
                "chipcard": _make_response(json_body=CHIPCARD_OK_BODY),
                "session-id-token": _make_response(json_body="2083728659"),
                "policies": _make_response(json_body=POLICIES_OK_BODY),
            }
        )
        client = CignaApiClient(context)

        user_ctx = await client.fetch_user_context("Z3512875K")

        assert user_ctx.chipcard == "010346461"
        assert user_ctx.group_code == "800251"
        assert user_ctx.id_member == 948849

        # Verify the chain: session-token-token received the chipcard,
        # policies/<token> received the right session token.
        urls_called = [c["url"] for c in context.captured_calls]
        assert any("chipcard" in u for u in urls_called)
        assert any("session-id-token" in u for u in urls_called)
        assert any("/policies/2083728659" in u for u in urls_called)

    async def test_failure_in_chipcard_short_circuits(self):
        context = _make_context(
            {
                "chipcard": _make_response(ok=False, status=500, text_body="error"),
                "session-id-token": _make_response(json_body="should-not-be-called"),
            }
        )
        client = CignaApiClient(context)
        with pytest.raises(CignaApiBadStatus):
            await client.fetch_user_context("Z3512875K")
        urls_called = [c["url"] for c in context.captured_calls]
        # only the chipcard call should have happened
        assert all("session-id-token" not in u for u in urls_called)
