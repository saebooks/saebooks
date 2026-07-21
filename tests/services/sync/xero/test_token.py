"""Tests for ``saebooks.services.sync.xero.token``.

Covers:

* Token refresh on first ``get()`` call.
* Caching: second call within TTL does not refresh.
* Forced refresh.
* Mandatory rotation invariant — every refresh response must include a
  new ``refresh_token`` (rotation), and the ``on_refresh_rotated``
  callback fires synchronously with the new value.
* Error mapping: 400 invalid_grant -> ``SyncAuthError``; missing
  refresh_token -> ``SyncAuthError``.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.sync.errors import SyncAuthError
from saebooks.services.sync.xero.token import (
    XERO_TOKEN_URL,
    XeroTokenCache,
    build_authorize_url,
    exchange_code_for_tokens,
)


def _ok_refresh(access: str, refresh: str, expires_in: int = 1800) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "token_type": "Bearer",
        },
    )


# ---------------------------------------------------------------------- #
# Refresh + caching                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_first_get_refreshes_and_caches() -> None:
    route = respx.post(XERO_TOKEN_URL).mock(
        return_value=_ok_refresh("ACCESS-1", "REFRESH-2"),
    )
    cache = XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="REFRESH-1",
    )
    try:
        token1 = await cache.get()
        token2 = await cache.get()
    finally:
        await cache.aclose()
    assert token1 == "ACCESS-1"
    assert token2 == "ACCESS-1"
    assert route.call_count == 1
    # Rotation persisted on the cache itself.
    assert cache.refresh_token == "REFRESH-2"


@respx.mock
async def test_force_refresh_bypasses_cache() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        side_effect=[
            _ok_refresh("ACCESS-1", "REFRESH-2"),
            _ok_refresh("ACCESS-2", "REFRESH-3"),
        ]
    )
    cache = XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="REFRESH-1",
    )
    try:
        first = await cache.get()
        forced = await cache.force_refresh()
    finally:
        await cache.aclose()
    assert first == "ACCESS-1"
    assert forced == "ACCESS-2"
    assert cache.refresh_token == "REFRESH-3"


@respx.mock
async def test_on_refresh_rotated_fires_synchronously() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        return_value=_ok_refresh("ACCESS-1", "ROTATED")
    )
    seen: list[str] = []

    async def cb(new: str) -> None:
        seen.append(new)

    cache = XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="OLD",
        on_refresh_rotated=cb,
    )
    try:
        await cache.get()
    finally:
        await cache.aclose()
    assert seen == ["ROTATED"]


# ---------------------------------------------------------------------- #
# Error paths                                                            #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_400_invalid_grant_raises_sync_auth_error() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": "invalid_grant"},
        )
    )
    cache = XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="dead",
    )
    with pytest.raises(SyncAuthError) as exc:
        await cache.get()
    assert exc.value.http_status == 400
    await cache.aclose()


@respx.mock
async def test_missing_refresh_token_violates_rotation_invariant() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "A", "expires_in": 1800},
        )
    )
    cache = XeroTokenCache(
        client_id="cid",
        client_secret="secret",
        refresh_token="OLD",
    )
    with pytest.raises(SyncAuthError, match="rotation invariant violated"):
        await cache.get()
    await cache.aclose()


# ---------------------------------------------------------------------- #
# Authorize URL + code exchange                                          #
# ---------------------------------------------------------------------- #


def test_build_authorize_url_includes_pkce_params() -> None:
    url = build_authorize_url(
        client_id="CID",
        redirect_uri="https://app.example/cb",
        scopes=["openid", "offline_access"],
        state="STATE",
        code_challenge="CHALLENGE",
    )
    assert url.startswith(
        "https://login.xero.com/identity/connect/authorize?"
    )
    assert "client_id=CID" in url
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=S256" in url
    assert "scope=openid+offline_access" in url
    assert "state=STATE" in url


@respx.mock
async def test_exchange_code_returns_tokens() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 1800,
                "id_token": "ID",
                "scope": "openid offline_access",
            },
        )
    )
    body = await exchange_code_for_tokens(
        client_id="CID",
        client_secret="SEC",
        code="CODE",
        code_verifier="VER",
        redirect_uri="https://app.example/cb",
    )
    assert body["access_token"] == "AT"
    assert body["refresh_token"] == "RT"


@respx.mock
async def test_exchange_code_missing_field_raises() -> None:
    respx.post(XERO_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "AT", "expires_in": 1800},
        )
    )
    with pytest.raises(SyncAuthError, match="refresh_token"):
        await exchange_code_for_tokens(
            client_id="CID",
            client_secret="SEC",
            code="CODE",
            code_verifier="VER",
            redirect_uri="https://app.example/cb",
        )
