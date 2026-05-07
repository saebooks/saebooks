"""Unit tests for saebooks.services.bank_feeds.client.

Uses respx to mock SISS HTTP responses.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.bank_feeds.client import SissClient
from saebooks.services.bank_feeds.errors import (
    SissAuthError,
    SissError,
    SissRateLimitError,
    SissScopeError,
    SissValidationError,
)
from saebooks.services.bank_feeds.token import TokenCache

TOKEN_URL = "https://auth.example/oauth/token"
API_BASE = "https://api.example/cdr-au/v1/"


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "TOK", "expires_in": 3600})


async def _make_client() -> SissClient:
    cache = TokenCache(
        client_id="cid",
        client_secret="secret",
        token_url=TOKEN_URL,
    )
    return SissClient(
        api_base=API_BASE,
        subscription_key="APIM-KEY",
        token_cache=cache,
    )


@respx.mock
async def test_get_attaches_required_headers_and_parses_json() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients").mock(
        return_value=httpx.Response(200, json={"data": [{"sdsClientId": "abc"}]})
    )
    client = await _make_client()
    async with client:
        body = await client.get("sds/clients")
    assert body == {"data": [{"sdsClientId": "abc"}]}
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer TOK"
    assert req.headers["Ocp-Apim-Subscription-Key"] == "APIM-KEY"
    assert req.headers["x-v"] == "1"
    assert "x-fapi-interaction-id" in req.headers
    # UUID4 format check: 36 chars with hyphens at known positions
    iid = req.headers["x-fapi-interaction-id"]
    assert len(iid) == 36 and iid[8] == "-" and iid[13] == "-"


@respx.mock
async def test_empty_response_body_returns_none() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.delete(API_BASE + "sds/clients/abc").mock(return_value=httpx.Response(204))
    client = await _make_client()
    async with client:
        body = await client.delete("sds/clients/abc")
    assert body is None


@respx.mock
async def test_401_triggers_token_refresh_and_retries_once() -> None:
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "STALE", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "FRESH", "expires_in": 3600}),
        ]
    )
    api_route = respx.get(API_BASE + "sds/clients").mock(
        side_effect=[
            httpx.Response(401, json={"errors": [{"code": "AUTH01", "title": "bad"}]}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = await _make_client()
    async with client:
        body = await client.get("sds/clients")
    assert body == {"ok": True}
    assert token_route.call_count == 2
    assert api_route.call_count == 2
    # Second API call should have used the fresh token
    second = api_route.calls[1].request
    assert second.headers["Authorization"] == "Bearer FRESH"


@respx.mock
async def test_401_twice_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(API_BASE + "sds/clients").mock(
        return_value=httpx.Response(401, json={"errors": [{"code": "AUTH01"}]})
    )
    client = await _make_client()
    async with client:
        with pytest.raises(SissAuthError) as exc:
            await client.get("sds/clients")
    assert exc.value.http_status == 401


@respx.mock
async def test_429_honours_retry_after_and_retries() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    api_route = respx.get(API_BASE + "sds/clients").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = await _make_client()
    async with client:
        body = await client.get("sds/clients")
    assert body == {"ok": True}
    assert api_route.call_count == 2


@respx.mock
async def test_429_exhausts_retries_then_raises_rate_limit_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(API_BASE + "sds/clients").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    client = await _make_client()
    async with client:
        with pytest.raises(SissRateLimitError) as exc:
            await client.get("sds/clients")
    assert exc.value.http_status == 429
    assert exc.value.retry_after_seconds == 0.0


@respx.mock
async def test_403_raises_scope_error_without_retry() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    api_route = respx.get(API_BASE + "sds/clients").mock(
        return_value=httpx.Response(
            403, json={"errors": [{"code": "SCOPE01", "title": "missing scope"}]}
        )
    )
    client = await _make_client()
    async with client:
        with pytest.raises(SissScopeError) as exc:
            await client.get("sds/clients")
    assert api_route.call_count == 1
    assert exc.value.errors[0].code == "SCOPE01"


@respx.mock
async def test_422_raises_validation_error_with_parsed_errors() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.post(API_BASE + "sds/account-access-consents/authorise").mock(
        return_value=httpx.Response(
            422,
            json={
                "errors": [
                    {"code": "0001", "title": "Invalid field", "detail": "bsb required"}
                ]
            },
        )
    )
    client = await _make_client()
    async with client:
        with pytest.raises(SissValidationError) as exc:
            await client.post(
                "sds/account-access-consents/authorise", json={"foo": "bar"}
            )
    assert exc.value.errors[0].detail == "bsb required"


@respx.mock
async def test_transport_error_raises_siss_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(API_BASE + "sds/clients").mock(side_effect=httpx.ConnectError("boom"))
    client = await _make_client()
    async with client:
        with pytest.raises(SissError) as exc:
            await client.get("sds/clients")
    assert exc.value.http_status == 0


@respx.mock
async def test_api_base_without_trailing_slash_is_normalised() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get("https://api.example/cdr-au/v1/sds/clients").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    cache = TokenCache(client_id="cid", client_secret="secret", token_url=TOKEN_URL)
    client = SissClient(
        api_base="https://api.example/cdr-au/v1",  # note: no trailing slash
        subscription_key="APIM-KEY",
        token_cache=cache,
    )
    async with client:
        await client.get("sds/clients")
    assert route.called


@respx.mock
async def test_params_are_forwarded() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/transactions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = await _make_client()
    async with client:
        await client.get(
            "sds/clients/abc/transactions",
            params={"fromTransactionId": "txn-123", "page-size": 50},
        )
    req = route.calls[0].request
    assert "fromTransactionId=txn-123" in str(req.url)
    assert "page-size=50" in str(req.url)
