"""Unit tests for saebooks.services.integrations.lei.client.

respx-mocks the GLEIF JSON:API endpoint. We don't hit the public
GLEIF API.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.config import Settings
from saebooks.services.integrations.lei.client import (
    LeiError,
    LeiNotFoundError,
    _normalise_lei,
    lookup_lei_raw,
)

LEI_BASE = "https://lei.example/api/v1"
SAMPLE_LEI = "529900T8BM49AURSDO55"


def _settings(**overrides: object) -> Settings:
    base = {"LEI_API_BASE": LEI_BASE}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_normalise_lei_strips_whitespace_and_uppercases() -> None:
    assert _normalise_lei("529900t8bm49aursdo55") == SAMPLE_LEI
    assert _normalise_lei(" 529900 T8BM 49AURSDO55 ") == SAMPLE_LEI


async def test_lookup_raises_on_malformed_lei() -> None:
    # too short
    with pytest.raises(LeiError, match="20 alphanumeric"):
        await lookup_lei_raw("abc", settings=_settings())
    # 20 chars but last 2 not digits
    with pytest.raises(LeiError, match="20 alphanumeric"):
        await lookup_lei_raw("ABCDEFGHIJKLMNOPQRXY", settings=_settings())


@respx.mock
async def test_lookup_returns_data_envelope_only() -> None:
    payload = {
        "data": {
            "type": "lei-records",
            "id": SAMPLE_LEI,
            "attributes": {
                "lei": SAMPLE_LEI,
                "entity": {
                    "legalName": {"name": "GlobalBank AG", "language": "en"},
                    "jurisdiction": "DE",
                    "status": "ACTIVE",
                },
                "registration": {"status": "ISSUED"},
            },
        }
    }
    route = respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await lookup_lei_raw(SAMPLE_LEI, settings=_settings())
    assert route.called
    # envelope stripped — the test sees data sub-object only
    assert result["id"] == SAMPLE_LEI
    assert result["attributes"]["entity"]["jurisdiction"] == "DE"


@respx.mock
async def test_lookup_normalises_before_request() -> None:
    payload = {
        "data": {
            "type": "lei-records",
            "id": SAMPLE_LEI,
            "attributes": {"lei": SAMPLE_LEI},
        }
    }
    route = respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    await lookup_lei_raw("529900t8bm49aursdo55", settings=_settings())
    assert route.called


@respx.mock
async def test_lookup_raises_lei_not_found_on_404() -> None:
    respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(404, text="not found")
    )
    with pytest.raises(LeiNotFoundError, match=SAMPLE_LEI):
        await lookup_lei_raw(SAMPLE_LEI, settings=_settings())


@respx.mock
async def test_lookup_raises_lei_error_on_500() -> None:
    respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(500, text="bad")
    )
    with pytest.raises(LeiError, match="HTTP 500"):
        await lookup_lei_raw(SAMPLE_LEI, settings=_settings())


@respx.mock
async def test_lookup_raises_on_non_json() -> None:
    respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(200, text="<html>error</html>")
    )
    with pytest.raises(LeiError, match="valid JSON"):
        await lookup_lei_raw(SAMPLE_LEI, settings=_settings())


@respx.mock
async def test_lookup_raises_on_missing_data_envelope() -> None:
    respx.get(f"{LEI_BASE}/lei-records/{SAMPLE_LEI}").mock(
        return_value=httpx.Response(200, json={"errors": [{"status": "500"}]})
    )
    with pytest.raises(LeiError, match="missing 'data' envelope"):
        await lookup_lei_raw(SAMPLE_LEI, settings=_settings())
