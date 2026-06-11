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

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

_FAKE_PDF = b"%PDF-1.5 fake po pdf"
_FAKE_PDF_URL = "/files/test-po.pdf"
_LATEX_API_BASE = "http://latex-api:8000"


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
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.contact_type == ContactType.SUPPLIER,
                ).limit(1)
            )
        ).scalars().first()
        if contact is None:
            company = (
                await session.execute(
                    select(Company).where(
                        Company.tenant_id == DEFAULT_TENANT_ID,
                        Company.archived_at.is_(None),
                    ).limit(1)
                )
            ).scalars().first()
            assert company is not None, "Seed company missing"
            contact = Contact(
                tenant_id=DEFAULT_TENANT_ID,
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
    import os

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE

    # Invalidate the cached Jinja env so it picks up the test LATEX_API_URL.
    import saebooks.services.latex_pdf as _svc

    _svc._env = None

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(
            200, json={"status": "ok", "pdf_url": _FAKE_PDF_URL, "id": "t1"}
        )
    )
    respx_mock.get(f"{_LATEX_API_BASE}{_FAKE_PDF_URL}").mock(
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
