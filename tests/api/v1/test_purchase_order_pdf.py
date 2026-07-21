"""Tests for GET /api/v1/purchase_orders/{po_id}/pdf.

Uses respx to mock latex-api — never hits the live service. Mirrors
tests/api/v1/test_statement_pack_pdf.py.

Tests:
* test_po_pdf_returns_pdf — POST a PO, GET its /pdf → 200 application/pdf
* test_po_pdf_404_on_missing — random UUID → 404
"""
from __future__ import annotations

import uuid

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

_FAKE_PDF = b"%PDF-1.5 fake po pdf"
_RENDER_BASE = "http://web:8080"


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def po_deps() -> dict[str, str]:
    """Provision an EXPENSE account id + SUPPLIER contact id (self-bootstrapping)."""
    from saebooks.services.companies import ensure_seed_company

    async with AsyncSessionLocal() as session:
        # Scope to the seed company (multi-company seed → tenant-only picks can
        # return a foreign-company account; see test_purchase_orders.po_deps).
        company = await ensure_seed_company(session)
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).order_by(Account.code).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.archived_at.is_(None),
                    Contact.contact_type == ContactType.SUPPLIER,
                ).limit(1)
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                tenant_id=company.tenant_id,
                company_id=company.id,
                name="PDF Test Vendor",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)

    assert expense is not None, "Test DB has no EXPENSE account in default tenant"
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


def _mock_latex(respx_mock: respx.MockRouter) -> None:
    """Mock the app render service — render_latex POSTs ctx and gets PDF bytes."""
    respx_mock.post(f"{_RENDER_BASE}/internal/render/purchase_order").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )


@pytest.mark.asyncio
async def test_po_pdf_returns_pdf(
    api_client: AsyncClient,
    po_deps: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    """POST a PO with two lines, then GET /pdf → 200 application/pdf."""
    _mock_latex(respx_mock)

    create = await api_client.post(
        "/api/v1/purchase_orders",
        json={
            "contact_id": po_deps["contact_id"],
            "issue_date": "2026-06-11",
            "expected_date": "2026-06-15",
            "notes": "Airbag please",
            "delivery_address": "SAE Engineering\r\n14 Ponzo St\r\nWoree\r\nQLD 4868",
            "lines": [
                {
                    "description": "ATV320 1.1kw VSD",
                    "account_id": po_deps["expense_account_id"],
                    "quantity": "1",
                    "unit_price": "252.38",
                    "discount_pct": "0",
                },
                {
                    "description": "Ethernet TCP-IP",
                    "account_id": po_deps["expense_account_id"],
                    "quantity": "1",
                    "unit_price": "220.82",
                    "discount_pct": "0",
                },
            ],
        },
    )
    assert create.status_code == 201, create.text
    po_id = create.json()["id"]

    resp = await api_client.get(f"/api/v1/purchase_orders/{po_id}/pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    # Draft POs have no number — the filename falls back to the id prefix.
    assert "Content-Disposition" in resp.headers
    assert ".pdf" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_po_pdf_404_on_missing(
    api_client: AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    _mock_latex(respx_mock)
    resp = await api_client.get(f"/api/v1/purchase_orders/{uuid.uuid4()}/pdf")
    assert resp.status_code == 404
