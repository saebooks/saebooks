"""Tests for ``saebooks.services.sync.xero.client.XeroClient``.

Covers:

* Bearer token + Xero-tenant-id headers on every request.
* 401-once-then-refresh-and-retry.
* 429 + Retry-After loop, surfacing ``SyncRateLimited`` after max
  retries.
* 304 -> empty body return.
* 4xx -> SyncValidationError; 5xx -> SyncUpstreamError.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.sync.errors import (
    SyncAuthError,
    SyncRateLimited,
    SyncUpstreamError,
    SyncValidationError,
)
from saebooks.services.sync.xero.client import XERO_API_BASE, XeroClient
from saebooks.services.sync.xero.token import XERO_TOKEN_URL, XeroTokenCache


def _ok_refresh(access: str = "ACCESS") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": "ROTATED",
            "expires_in": 1800,
        },
    )


def _make_cache() -> XeroTokenCache:
    return XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="OLD",
    )


@respx.mock
async def test_get_injects_bearer_and_tenant_headers() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    route = respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(200, json={"Contacts": []}),
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN-1",
    ) as client:
        body, _headers = await client.get("Contacts")
    assert body == {"Contacts": []}
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer ACCESS"
    assert req.headers["Xero-tenant-id"] == "TEN-1"


@respx.mock
async def test_401_triggers_one_refresh_and_retries() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        side_effect=[_ok_refresh("OLD-A"), _ok_refresh("NEW-A")],
    )
    route = respx.get(XERO_API_BASE + "Contacts").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(200, json={"Contacts": [{"ContactID": "X"}]}),
        ]
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        body, _ = await client.get("Contacts")
    assert body["Contacts"][0]["ContactID"] == "X"
    assert route.call_count == 2
    # Second call carried the refreshed token.
    assert route.calls[1].request.headers["Authorization"] == "Bearer NEW-A"


@respx.mock
async def test_persistent_401_raises_sync_auth_error() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        side_effect=[_ok_refresh(), _ok_refresh()],
    )
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(401, json={"detail": "still expired"}),
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        with pytest.raises(SyncAuthError):
            await client.get("Contacts")


@respx.mock
async def test_429_retry_then_succeeds() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    route = respx.get(XERO_API_BASE + "Contacts").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"Contacts": []}),
        ]
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        body, _ = await client.get("Contacts")
    assert body == {"Contacts": []}
    assert route.call_count == 2


@respx.mock
async def test_429_max_retries_raises_rate_limited() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}),
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        with pytest.raises(SyncRateLimited):
            await client.get("Contacts")


@respx.mock
async def test_304_returns_empty_body() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(304),
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        body, _ = await client.get(
            "Contacts",
            if_modified_since="2026-05-01T00:00:00",
        )
    assert body == {}


@respx.mock
async def test_400_raises_sync_validation_error() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.post(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            400,
            json={"ValidationErrors": [{"Message": "bad"}]},
        )
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        with pytest.raises(SyncValidationError) as exc:
            await client.post("Contacts", json={"Contacts": [{}]})
    assert exc.value.http_status == 400


@respx.mock
async def test_500_raises_sync_upstream_error() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(500, text="oops")
    )
    async with XeroClient(
        token_cache=_make_cache(),
        xero_tenant_id="TEN",
    ) as client:
        with pytest.raises(SyncUpstreamError) as exc:
            await client.get("Contacts")
    assert exc.value.http_status == 500
