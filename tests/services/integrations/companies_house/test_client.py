"""Unit tests for saebooks.services.integrations.companies_house.client.

respx-mocks the CH public-information API. We don't hit the live
https://api.company-information.service.gov.uk endpoint.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.config import Settings
from saebooks.services.integrations.companies_house.client import (
    CompaniesHouseError,
    CompaniesHouseNotConfiguredError,
    CompaniesHouseNotFoundError,
    _normalise_number,
    lookup_company_raw,
)

CH_BASE = "https://ch.example/api"
SAMPLE_NUMBER = "00000006"


def _settings(**overrides: object) -> Settings:
    base = {"CH_API_KEY": "test-ch-key", "CH_API_BASE": CH_BASE}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_normalise_zero_pads_short_numeric() -> None:
    assert _normalise_number("6") == "00000006"
    assert _normalise_number("000006") == "00000006"
    assert _normalise_number("12345678") == "12345678"


def test_normalise_uppercases_and_strips_whitespace() -> None:
    assert _normalise_number("  sc 123 456  ") == "SC123456"
    assert _normalise_number("ni987654") == "NI987654"


def test_normalise_preserves_alpha_prefix_and_pads_body() -> None:
    # SC123 -> SC00123 (body padded to 6 = 8 - len("SC"))
    assert _normalise_number("SC123") == "SC000123"
    assert _normalise_number("NI12") == "NI000012"


def test_normalise_rejects_empty() -> None:
    with pytest.raises(CompaniesHouseError, match="empty"):
        _normalise_number("")
    with pytest.raises(CompaniesHouseError, match="empty"):
        _normalise_number("   ")


async def test_lookup_raises_when_api_key_missing() -> None:
    with pytest.raises(CompaniesHouseNotConfiguredError):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings(CH_API_KEY=""))


async def test_lookup_raises_on_malformed_number() -> None:
    # 9 chars after normalisation — invalid
    with pytest.raises(CompaniesHouseError, match="8 alphanumeric"):
        await lookup_company_raw("TOOLONG123", settings=_settings())


@respx.mock
async def test_lookup_returns_parsed_json_body() -> None:
    payload = {
        "company_name": "CROWN AGENTS FOUNDATION",
        "company_number": SAMPLE_NUMBER,
        "company_status": "active",
    }
    route = respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())
    assert route.called
    assert result["company_name"] == "CROWN AGENTS FOUNDATION"
    assert result["company_number"] == SAMPLE_NUMBER


@respx.mock
async def test_lookup_normalises_before_request() -> None:
    # Short numeric form should be zero-padded in the URL path.
    payload = {"company_number": SAMPLE_NUMBER, "company_name": "X"}
    route = respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    await lookup_company_raw("6", settings=_settings())
    assert route.called


@respx.mock
async def test_lookup_uses_basic_auth_with_empty_password() -> None:
    """CH auth quirk: API key is the username, password is empty."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"company_number": SAMPLE_NUMBER})

    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(side_effect=_capture)
    await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())
    # "test-ch-key:" base64-encoded is "dGVzdC1jaC1rZXk6"
    assert captured["auth"].startswith("Basic ")
    assert captured["auth"] == "Basic dGVzdC1jaC1rZXk6"


@respx.mock
async def test_lookup_raises_not_found_on_404() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(404, text="")
    )
    with pytest.raises(CompaniesHouseNotFoundError, match=SAMPLE_NUMBER):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())


@respx.mock
async def test_lookup_raises_on_401_bad_key() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(401, text="")
    )
    with pytest.raises(CompaniesHouseError, match=r"HTTP 401|CH_API_KEY"):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())


@respx.mock
async def test_lookup_raises_on_429_rate_limit() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(429, text="")
    )
    with pytest.raises(CompaniesHouseError, match=r"429|rate"):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())


@respx.mock
async def test_lookup_raises_on_500() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(500, text="server error")
    )
    with pytest.raises(CompaniesHouseError, match="HTTP 500"):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())


@respx.mock
async def test_lookup_raises_on_non_json_body() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(200, text="<html>bad</html>")
    )
    with pytest.raises(CompaniesHouseError, match="valid JSON"):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())


@respx.mock
async def test_lookup_raises_when_body_is_not_a_dict() -> None:
    respx.get(f"{CH_BASE}/company/{SAMPLE_NUMBER}").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    with pytest.raises(CompaniesHouseError, match="not a JSON object"):
        await lookup_company_raw(SAMPLE_NUMBER, settings=_settings())
