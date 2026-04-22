"""Tests for the ATO SBR reachability ping.

Pure respx-mocked HTTP tests — we don't hit the real ATO endpoints.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.config import Settings
from saebooks.services.ato_sbr.ping import ping_environment

EVTE = "https://evte.example"
PROD = "https://prod.example"


def _s() -> Settings:
    return Settings(
        ATO_SBR_EVTE_BASE=EVTE,
        ATO_SBR_PROD_BASE=PROD,
    )


@pytest.mark.asyncio
@respx.mock
async def test_ping_evte_returns_ok_on_200() -> None:
    respx.get(EVTE).mock(return_value=httpx.Response(200, text="hi"))
    result = await ping_environment("evte", settings=_s())
    assert result.ok is True
    assert result.status_code == 200
    assert result.url == EVTE


@pytest.mark.asyncio
@respx.mock
async def test_ping_production_hits_prod_url() -> None:
    respx.get(PROD).mock(return_value=httpx.Response(200))
    result = await ping_environment("production", settings=_s())
    assert result.ok is True
    assert result.url == PROD


@pytest.mark.asyncio
@respx.mock
async def test_ping_unknown_environment_falls_back_to_evte() -> None:
    # Anything that isn't 'production' is treated as EVTE. Defensive
    # default — we never want a typo to lodge against production.
    respx.get(EVTE).mock(return_value=httpx.Response(403))
    result = await ping_environment("garbage", settings=_s())
    assert result.url == EVTE


@pytest.mark.asyncio
@respx.mock
async def test_403_counts_as_reachable() -> None:
    """4xx from the ATO gateway still proves the network + TLS worked."""
    respx.get(EVTE).mock(return_value=httpx.Response(403))
    result = await ping_environment("evte", settings=_s())
    assert result.ok is True
    assert result.status_code == 403


@pytest.mark.asyncio
@respx.mock
async def test_500_counts_as_fail() -> None:
    respx.get(EVTE).mock(return_value=httpx.Response(500))
    result = await ping_environment("evte", settings=_s())
    assert result.ok is False
    assert result.status_code == 500


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_surfaces_detail() -> None:
    respx.get(EVTE).mock(side_effect=httpx.ConnectError("no route"))
    result = await ping_environment("evte", settings=_s())
    assert result.ok is False
    assert result.status_code is None
    assert "ConnectError" in result.detail
