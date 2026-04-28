"""Parametrised hard-delete admin-gate test for every DELETE route.

The 20 routes that ship with ?hard=true. We can't easily build a fixture
row for every entity in one place (different POST shapes, FK chains,
period locks, etc.) — but we CAN verify that the admin gate runs BEFORE
any lookup. So a non-admin DELETE with ?hard=true on a random UUID
always returns 403, never 404. That is the contract that protects every
route uniformly. The full end-to-end (admin can hard-delete a real row,
audit_log appears) is covered for the highest-risk entities by
test_hard_delete_invoice and test_hard_delete_company_cascade.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app


HARD_DELETE_ROUTES: tuple[str, ...] = (
    "account_ranges",
    "accounts",
    "allocations",
    "bank_accounts",
    "bank_rules",
    "bank_statement_lines",
    "bills",
    "budgets",
    "contacts",
    "credit_notes",
    "fixed_assets",
    "invoices",
    "items",
    "journal_entries",
    "journal_templates",
    "payments",
    "projects",
    "recurring_invoices",
    "tax_codes",
    "users",
    "companies",
)


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.mark.parametrize("resource", HARD_DELETE_ROUTES)
async def test_hard_delete_no_admin_returns_403(
    api_client: AsyncClient, resource: str
) -> None:
    """The admin gate runs before any DB lookup → 403, never 404."""
    fake = uuid.uuid4()
    r = await api_client.delete(f"/api/v1/{resource}/{fake}?hard=true")
    assert r.status_code == 403, (
        f"/{resource} returned {r.status_code} (expected 403): {r.text}"
    )


@pytest.mark.parametrize("resource", HARD_DELETE_ROUTES)
async def test_hard_delete_admin_unknown_id_returns_404(
    api_client: AsyncClient, resource: str
) -> None:
    """Admin clears the gate — then the row lookup 404s for a random UUID."""
    fake = uuid.uuid4()
    r = await api_client.delete(
        f"/api/v1/{resource}/{fake}?hard=true",
        headers={"X-Admin": "true"},
    )
    # Companies passes the admin gate, then 404 on the lookup. Same for
    # every other route — they all "verify the row" before reaching
    # hard_delete_with_audit. 422 also acceptable for routes that do
    # version-locking inside the soft-delete branch (we never reach
    # that branch because hard=true short-circuits earlier).
    assert r.status_code == 404, (
        f"/{resource} returned {r.status_code} (expected 404): {r.text}"
    )
