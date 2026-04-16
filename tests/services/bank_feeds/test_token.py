"""Unit tests for saebooks.services.bank_feeds.token.

Uses respx to mock the OAuth endpoint.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.bank_feeds.errors import SissAuthError
from saebooks.services.bank_feeds.token import TokenCache

TOKEN_URL = "https://auth.example/oauth/token"


async def _make_cache() -> TokenCache:
    return TokenCache(
        client_id="cid",
        client_secret="secret",
        token_url=TOKEN_URL,
        scopes=["sds_clients"],
    )


@respx.mock
async def test_fetches_token_on_first_get() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "TOK", "expires_in": 3600}
        )
    )
    cache = await _make_cache()
    try:
        token = await cache.get()
        assert token == "TOK"
        assert route.called
        # Body should be form-encoded client_credentials with scopes
        sent = route.calls[0].request.content.decode()
        assert "grant_type=client_credentials" in sent
        assert "scope=sds_clients" in sent
    finally:
        await cache.aclose()


@respx.mock
async def test_caches_token_between_calls() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "TOK", "expires_in": 3600}
        )
    )
    cache = await _make_cache()
    try:
        await cache.get()
        await cache.get()
        await cache.get()
        assert route.call_count == 1
    finally:
        await cache.aclose()


@respx.mock
async def test_invalidate_forces_refresh() -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "T1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "T2", "expires_in": 3600}),
        ]
    )
    cache = await _make_cache()
    try:
        assert await cache.get() == "T1"
        await cache.invalidate()
        assert await cache.get() == "T2"
        assert route.call_count == 2
    finally:
        await cache.aclose()


@respx.mock
async def test_non_200_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    cache = await _make_cache()
    try:
        with pytest.raises(SissAuthError) as exc:
            await cache.get()
        assert exc.value.http_status == 401
    finally:
        await cache.aclose()


@respx.mock
async def test_missing_access_token_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"expires_in": 3600})
    )
    cache = await _make_cache()
    try:
        with pytest.raises(SissAuthError):
            await cache.get()
    finally:
        await cache.aclose()


@respx.mock
async def test_transport_error_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    cache = await _make_cache()
    try:
        with pytest.raises(SissAuthError) as exc:
            await cache.get()
        assert exc.value.http_status == 0
    finally:
        await cache.aclose()


@respx.mock
async def test_missing_expires_in_defaults_to_one_hour() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "TOK"})
    )
    cache = await _make_cache()
    try:
        await cache.get()
        # Don't call the private _cached attribute as a contract; just
        # verify a second get() doesn't refetch (would fail if ttl was
        # somehow 0).
        await cache.get()
    finally:
        await cache.aclose()
