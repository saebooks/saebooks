"""Web tests for the year-end-close ADMIN gate (Phase-1 #3).

POST /reports/close-year locks the period and is effectively
irreversible, so it is gated with ``require_role(ADMIN)``. The GET
preview stays open (read-only). These tests prove the gate without
mutating ledger state (the admin path is sent an empty body so it
stops at 400 validation, after the gate, before any post).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_close_year_get_preview_is_open(client: AsyncClient) -> None:
    r = await client.get("/reports/close-year")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_close_year_post_rejects_non_admin(client: AsyncClient) -> None:
    # No authenticated user → the role gate rejects before the handler runs.
    r = await client.post("/reports/close-year", data={}, follow_redirects=False)
    assert r.status_code in (401, 403), r.text


@pytest.mark.asyncio
async def test_close_year_post_admin_passes_gate(admin_client: AsyncClient) -> None:
    # Admin clears the gate and reaches handler validation; an empty body
    # → 400 (missing through/retained), which proves the gate let the
    # request through WITHOUT posting/locking anything.
    r = await admin_client.post(
        "/reports/close-year", data={}, follow_redirects=False
    )
    assert r.status_code == 400, r.text
