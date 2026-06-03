"""Web smoke for the financial statement pack (Phase-1 #6, interim).

GET /reports/statement-pack must render a single document bundling the
P&L, Balance Sheet and Trial Balance with a cover + trustee declaration,
and offer the shared Print / Save-as-PDF affordance. HTML report routes
require no auth (see test_reports_auth_regression), so the plain client
fixture suffices.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_statement_pack_renders(client: AsyncClient) -> None:
    r = await client.get("/reports/statement-pack")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Special Purpose Financial Statements" in body
    assert "Statement of Profit or Loss" in body
    assert "Statement of Financial Position" in body
    assert "Trial Balance" in body
    assert "declaration" in body.lower()
    # Reuses the shared print include.
    assert "Print / Save as PDF" in body


@pytest.mark.asyncio
async def test_statement_pack_respects_explicit_period(client: AsyncClient) -> None:
    r = await client.get(
        "/reports/statement-pack",
        params={"as_of": "2025-06-30", "from": "2024-07-01", "to": "2025-06-30"},
    )
    assert r.status_code == 200, r.text
    assert "2025-06-30" in r.text
    assert "2024-07-01" in r.text
