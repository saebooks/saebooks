"""Unit tests for saebooks.services.abr.client.

respx-mocks the ABR JSON endpoint. We don't hit the public ABR API.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.config import Settings
from saebooks.services.abr.client import (
    AbrError,
    AbrNotConfiguredError,
    _normalise_abn,
    _strip_jsonp,
    lookup_abn_raw,
)

ABR_BASE = "https://abr.example/json"


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance. Settings uses env aliases, so pass
    the alias names — ``ABR_API_GUID`` rather than ``abr_api_guid``."""
    base = {
        "ABR_API_GUID": "test-guid",
        "ABR_API_BASE": ABR_BASE,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_strip_jsonp_removes_callback_wrapper() -> None:
    assert _strip_jsonp('callback({"Abn":"123"})') == '{"Abn":"123"}'


def test_strip_jsonp_leaves_bare_json_alone() -> None:
    assert _strip_jsonp('{"Abn":"123"}') == '{"Abn":"123"}'


def test_normalise_abn_strips_spaces_and_non_digits() -> None:
    assert _normalise_abn("51 824 753 556") == "51824753556"
    assert _normalise_abn("ABN: 51-824-753-556") == "51824753556"


async def test_lookup_raises_when_guid_not_configured() -> None:
    s = _settings(ABR_API_GUID="")
    with pytest.raises(AbrNotConfiguredError):
        await lookup_abn_raw("51824753556", settings=s)


async def test_lookup_raises_on_short_abn() -> None:
    with pytest.raises(AbrError, match="11 digits"):
        await lookup_abn_raw("123", settings=_settings())


@respx.mock
async def test_lookup_returns_parsed_envelope() -> None:
    payload = (
        'callback({'
        '"Abn":"51824753556",'
        '"AbnStatus":"Active",'
        '"EntityName":"Example Pty Ltd",'
        '"AddressState":"QLD",'
        '"AddressPostcode":"4350",'
        '"Gst":"2024-02-15",'
        '"Message":""'
        '})'
    )
    route = respx.get(f"{ABR_BASE}/AbnDetails.aspx").mock(
        return_value=httpx.Response(200, text=payload)
    )
    result = await lookup_abn_raw("51 824 753 556", settings=_settings())
    assert route.called
    assert result["Abn"] == "51824753556"
    assert result["EntityName"] == "Example Pty Ltd"
    # GUID + normalised ABN threaded through as query params
    called = route.calls.last.request.url.params
    assert called["abn"] == "51824753556"
    assert called["guid"] == "test-guid"


@respx.mock
async def test_lookup_raises_on_abr_error_message() -> None:
    payload = (
        'callback({"Abn":"","AbnStatus":"",'
        '"Message":"Search text is not a valid ABN or ACN"})'
    )
    respx.get(f"{ABR_BASE}/AbnDetails.aspx").mock(
        return_value=httpx.Response(200, text=payload)
    )
    with pytest.raises(AbrError, match="not a valid ABN"):
        await lookup_abn_raw("51824753556", settings=_settings())


@respx.mock
async def test_lookup_raises_on_http_500() -> None:
    respx.get(f"{ABR_BASE}/AbnDetails.aspx").mock(
        return_value=httpx.Response(500, text="down")
    )
    with pytest.raises(AbrError, match="HTTP 500"):
        await lookup_abn_raw("51824753556", settings=_settings())
