"""Tests for sandbox config fields and siss_client() sandbox routing.

Covers:
- siss_sandbox_key read from SISS_SANDBOX_PRIMARY_KEY env var
- siss_base_url read from SISS_BASE_URL env var, default points to sandbox host
- siss_client() uses sandbox key + base URL when siss_sandbox=True and sandbox
  key is set
- siss_client() falls back to production key + base URL when sandbox key is
  absent even if siss_sandbox=True
- siss_client() raises SissNotConfiguredError when core creds are missing

No real HTTP is made — respx mocks all network calls.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from saebooks.config import Settings
from saebooks.services.bank_feeds import onboarding
from saebooks.services.bank_feeds.onboarding import SissNotConfiguredError


# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #

_SANDBOX_URL = "https://sandboxapi.sissdata.com.au/cdr-au/v1/"
_PROD_URL = "https://api.sissdata.com.au/cdr-au/v1/"
_TOKEN_URL = "https://auth.example/oauth/token"


def _settings(**overrides: Any) -> Settings:
    """Settings with core SISS creds populated."""
    base: dict[str, Any] = dict(
        SISS_CLIENT_ID="cid",
        SISS_CLIENT_SECRET="csecret",
        SISS_SUBSCRIPTION_KEY="prod-key",
        SISS_TOKEN_URL=_TOKEN_URL,
        SISS_API_BASE=_PROD_URL,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "TOK", "expires_in": 3600})


# ---------------------------------------------------------------------- #
# Config field tests                                                      #
# ---------------------------------------------------------------------- #


def test_siss_sandbox_key_read_from_env_var() -> None:
    """SISS_SANDBOX_PRIMARY_KEY maps to siss_sandbox_key."""
    s = _settings(SISS_SANDBOX_PRIMARY_KEY="sandbox-apim-key")
    assert s.siss_sandbox_key == "sandbox-apim-key"


def test_siss_sandbox_key_defaults_to_empty_string() -> None:
    s = _settings()
    assert s.siss_sandbox_key == ""


def test_siss_base_url_defaults_to_sandbox_host() -> None:
    """Default siss_base_url points to the sandbox CDR API host."""
    s = _settings()
    assert "sandboxapi.sissdata.com.au" in s.siss_base_url


def test_siss_base_url_can_be_overridden() -> None:
    s = _settings(SISS_BASE_URL="https://custom.example/cdr-au/v1/")
    assert s.siss_base_url == "https://custom.example/cdr-au/v1/"


def test_siss_api_base_default_is_production() -> None:
    """siss_api_base (the production field) still defaults to the prod host."""
    s = Settings()  # type: ignore[call-arg]
    assert "sandboxapi" not in s.siss_api_base
    assert "sissdata.com.au" in s.siss_api_base


# ---------------------------------------------------------------------- #
# siss_client() sandbox routing                                           #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_siss_client_uses_sandbox_key_when_sandbox_flag_and_key_set() -> None:
    """With siss_sandbox=True + siss_sandbox_key set, the sandbox APIM key is used."""
    token_route = respx.post(_TOKEN_URL).mock(return_value=_token_response())
    api_route = respx.get(_SANDBOX_URL + "sds/clients").mock(
        return_value=httpx.Response(200, json={"data": {"clients": []}})
    )
    s = _settings(
        SISS_SANDBOX=True,
        SISS_SANDBOX_PRIMARY_KEY="sandbox-key-123",
        SISS_API_BASE=_PROD_URL,
        SISS_BASE_URL=_SANDBOX_URL,
    )
    async with onboarding.siss_client(settings=s) as client:
        await client.get("sds/clients")

    assert token_route.called
    req = api_route.calls[0].request
    assert req.headers["Ocp-Apim-Subscription-Key"] == "sandbox-key-123"
    # Should have hit the sandbox URL
    assert "sandboxapi.sissdata.com.au" in str(req.url)


@respx.mock
async def test_siss_client_uses_prod_key_when_sandbox_flag_but_no_sandbox_key() -> None:
    """siss_sandbox=True with no sandbox key falls back to production key."""
    token_route = respx.post(_TOKEN_URL).mock(return_value=_token_response())
    api_route = respx.get(_PROD_URL + "sds/clients").mock(
        return_value=httpx.Response(200, json={"data": {"clients": []}})
    )
    s = _settings(
        SISS_SANDBOX=True,
        # No SISS_SANDBOX_PRIMARY_KEY — falls back to production pair
        SISS_API_BASE=_PROD_URL,
    )
    async with onboarding.siss_client(settings=s) as client:
        await client.get("sds/clients")

    assert token_route.called
    req = api_route.calls[0].request
    assert req.headers["Ocp-Apim-Subscription-Key"] == "prod-key"
    assert "sandboxapi" not in str(req.url)


@respx.mock
async def test_siss_client_uses_prod_key_when_sandbox_flag_false() -> None:
    """Production path: sandbox flag off uses siss_subscription_key + siss_api_base."""
    token_route = respx.post(_TOKEN_URL).mock(return_value=_token_response())
    api_route = respx.get(_PROD_URL + "sds/clients").mock(
        return_value=httpx.Response(200, json={"data": {"clients": []}})
    )
    s = _settings(
        SISS_SANDBOX=False,
        SISS_SANDBOX_PRIMARY_KEY="should-not-be-used",
        SISS_API_BASE=_PROD_URL,
    )
    async with onboarding.siss_client(settings=s) as client:
        await client.get("sds/clients")

    req = api_route.calls[0].request
    assert req.headers["Ocp-Apim-Subscription-Key"] == "prod-key"


async def test_siss_client_raises_when_core_creds_missing() -> None:
    """siss_client() raises SissNotConfiguredError when core creds absent."""
    s = Settings(SISS_SANDBOX_PRIMARY_KEY="sandbox-key")  # type: ignore[call-arg]
    # No SISS_CLIENT_ID / SECRET / SUBSCRIPTION_KEY
    with pytest.raises(SissNotConfiguredError):
        async with onboarding.siss_client(settings=s):
            pass
