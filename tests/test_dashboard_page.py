"""Router smoke tests for ``/dashboard``.

* `/` redirects to `/dashboard`
* `/dashboard` renders 200 even on a near-empty DB
* Every widget appears (widget CSS classes in the rendered HTML)
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_redirects_to_dashboard(client: AsyncClient) -> None:
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_dashboard_renders(client: AsyncClient) -> None:
    r = await client.get("/dashboard")
    assert r.status_code == 200
    # Page header + each widget marker.
    assert "<h1>Dashboard</h1>" in r.text
    assert "widget-bank-balances" in r.text
    assert "widget-aged-ar" in r.text
    assert "widget-unmatched" in r.text
    assert "widget-cashflow" in r.text
    assert "widget-upcoming" in r.text


@pytest.mark.asyncio
async def test_dashboard_renders_with_trailing_slash(client: AsyncClient) -> None:
    r = await client.get("/dashboard/")
    assert r.status_code == 200
    assert "<h1>Dashboard</h1>" in r.text


@pytest.mark.asyncio
async def test_dashboard_includes_sparkline_svg(client: AsyncClient) -> None:
    """The sparkline must be inline SVG — no JS lib, no external fetch."""
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert "<svg" in r.text
    # 30-day window reported in the section header.
    assert "Cashflow" in r.text
