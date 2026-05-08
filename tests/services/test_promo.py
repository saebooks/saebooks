"""Tests for the launch-promo service.

Covers:
- attempt_promo returns None when flag is off (no HTTP call made)
- attempt_promo returns token on 200 response from license-server
- attempt_promo returns None on 410 (exhausted)
- attempt_promo returns None on 503 (flag off server-side)
- attempt_promo returns None on network error
- get_promo_stats returns disabled shape when flag off
- get_promo_stats returns live data from license-server
- get_promo_stats returns safe fallback on network error
"""
from __future__ import annotations

import pytest
import respx
import httpx

from saebooks.config import Settings
from saebooks.services import launch_promo as _mod


def _patch_settings(enabled: bool = False, limit: int = 1000, url: str = "http://license-test") -> Settings:
    """Return a Settings-like object for patching in tests."""
    # We monkey-patch the module's _settings reference.
    cfg = Settings(
        LAUNCH_PROMO_ENABLED=str(enabled).lower(),
        LAUNCH_PROMO_LIMIT=str(limit),
        LICENSE_SERVER_URL=url,
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
    )
    return cfg


# ---------------------------------------------------------------------------
# attempt_promo
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_attempt_promo_flag_off_returns_none(monkeypatch):
    """When LAUNCH_PROMO_ENABLED=false, no HTTP call is made."""
    cfg = _patch_settings(enabled=False)
    monkeypatch.setattr(_mod, "_settings", cfg)

    result = await _mod.attempt_promo(email="a@example.com", licensed_to="Acme")
    assert result is None


@pytest.mark.anyio
@respx.mock
async def test_attempt_promo_returns_token_on_200(monkeypatch):
    """Happy path: 200 from license-server → JWT returned."""
    cfg = _patch_settings(enabled=True, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.post("http://license-test/api/v1/license/issue-launch-promo").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "header.payload.sig",
                "edition": "pro",
                "license_id": "lic_01ABCD",
                "expires_at": "2027-05-08T00:00:00+00:00",
                "promo": "first-1000-launch",
                "promo_slot": 1,
                "idempotent": False,
            },
        )
    )

    result = await _mod.attempt_promo(email="user@example.com", licensed_to="Acme Pty Ltd")
    assert result == "header.payload.sig"


@pytest.mark.anyio
@respx.mock
async def test_attempt_promo_exhausted_returns_none(monkeypatch):
    """410 Gone from license-server (counter exhausted) → None."""
    cfg = _patch_settings(enabled=True, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.post("http://license-test/api/v1/license/issue-launch-promo").mock(
        return_value=httpx.Response(410, json={"error": "promo_exhausted"})
    )

    result = await _mod.attempt_promo(email="late@example.com", licensed_to="Late Co")
    assert result is None


@pytest.mark.anyio
@respx.mock
async def test_attempt_promo_server_off_returns_none(monkeypatch):
    """503 from license-server (promo disabled server-side) → None."""
    cfg = _patch_settings(enabled=True, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.post("http://license-test/api/v1/license/issue-launch-promo").mock(
        return_value=httpx.Response(503, json={"error": "promo_not_active"})
    )

    result = await _mod.attempt_promo(email="early@example.com", licensed_to="Early Co")
    assert result is None


@pytest.mark.anyio
@respx.mock
async def test_attempt_promo_network_error_returns_none(monkeypatch):
    """Network error → None, never raises."""
    cfg = _patch_settings(enabled=True, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.post("http://license-test/api/v1/license/issue-launch-promo").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    result = await _mod.attempt_promo(email="bad@example.com", licensed_to="Bad Net Co")
    assert result is None


@pytest.mark.anyio
@respx.mock
async def test_attempt_promo_bad_json_returns_none(monkeypatch):
    """200 with non-JSON body → None."""
    cfg = _patch_settings(enabled=True, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.post("http://license-test/api/v1/license/issue-launch-promo").mock(
        return_value=httpx.Response(200, content=b"not json")
    )

    result = await _mod.attempt_promo(email="bad@example.com", licensed_to="Bad Co")
    assert result is None


# ---------------------------------------------------------------------------
# get_promo_stats
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_promo_stats_flag_off(monkeypatch):
    """When flag off, stats show enabled=false without hitting license-server."""
    cfg = _patch_settings(enabled=False, limit=1000)
    monkeypatch.setattr(_mod, "_settings", cfg)

    stats = await _mod.get_promo_stats()
    assert stats["enabled"] is False
    assert stats["issued"] == 0
    assert stats["limit"] == 1000
    assert stats["remaining"] == 1000


@pytest.mark.anyio
@respx.mock
async def test_get_promo_stats_live(monkeypatch):
    """Flag on → fetches from license-server and returns live data."""
    cfg = _patch_settings(enabled=True, limit=1000, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.get("http://license-test/api/v1/license/promo-stats").mock(
        return_value=httpx.Response(
            200,
            json={"enabled": True, "issued": 42, "limit": 1000, "remaining": 958},
        )
    )

    stats = await _mod.get_promo_stats()
    assert stats["enabled"] is True
    assert stats["issued"] == 42
    assert stats["remaining"] == 958


@pytest.mark.anyio
@respx.mock
async def test_get_promo_stats_network_fallback(monkeypatch):
    """Network error → safe fallback dict, never raises."""
    cfg = _patch_settings(enabled=True, limit=1000, url="http://license-test")
    monkeypatch.setattr(_mod, "_settings", cfg)

    respx.get("http://license-test/api/v1/license/promo-stats").mock(
        side_effect=httpx.ConnectError("refused")
    )

    stats = await _mod.get_promo_stats()
    assert "error" in stats
    assert stats["limit"] == 1000
