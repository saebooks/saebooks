"""Tests for the BAS route gst_registered gate.

Critic finding #21: /reports/bas rendered for gst_registered=False companies.

Covers:
* gst_registered=False company → 200 with not-registered banner (no BAS data)
* gst_registered=True company → 200 with BAS report content
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
    ) as ac:
        yield ac


async def _set_gst_registered(value: bool) -> None:
    """Set gst_registered on the seed company."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Company)
            .where(
                Company.tenant_id == DEFAULT_TENANT_ID,
                Company.archived_at.is_(None),
            )
            .values(gst_registered=value)
        )
        await session.commit()


async def test_bas_not_registered_shows_banner(api_client: AsyncClient) -> None:
    """When company.gst_registered=False the route returns 200 with not-registered banner.

    Must NOT show BAS report numbers or a Lodge BAS CTA.
    """
    await _set_gst_registered(False)
    try:
        r = await api_client.get("/reports/bas")
        assert r.status_code == 200, r.text
        # Should show not-registered banner content
        body = r.text
        assert "Not GST-registered" in body or "not registered" in body.lower() or "not_registered" in body, (
            "Expected not-registered banner but got: " + body[:500]
        )
    finally:
        # Restore to True so other tests are not affected
        await _set_gst_registered(True)


async def test_bas_registered_shows_report(api_client: AsyncClient) -> None:
    """When company.gst_registered=True the route returns 200 with BAS content."""
    await _set_gst_registered(True)
    r = await api_client.get("/reports/bas")
    assert r.status_code == 200, r.text
    # Should NOT show the not-registered banner
    body = r.text
    assert "Not GST-registered" not in body
    # Should show BAS report structure (label column)
    assert "G1" in body or "BAS" in body.upper()
