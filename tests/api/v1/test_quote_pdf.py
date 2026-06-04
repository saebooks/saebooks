"""Tests for GET /api/v1/quotes/{id}/pdf — LaTeX engine.

Two tests:
1. test_quote_pdf_returns_pdf   — 200 application/pdf with mocked latex-api.
2. test_quote_pdf_404           — unknown UUID → 404.

latex-api is mocked with respx; the test DB is seeded with a real quote.
The quote includes two section_label groups and special characters in
description and customer name to exercise latex_escape paths.
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

_FAKE_PDF = b"%PDF-1.5 fake-quote-pdf"
_FAKE_PDF_URL = "/files/quote-test.pdf"
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
async def quote_id() -> str:
    """Create a quote with two section groups and special chars; return its UUID."""

    token = current_token()
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                ).order_by(Company.created_at).limit(1)
            )
        ).scalars().first()
        assert company is not None, "Seed company missing"

        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                    Account.company_id == company.id,
                ).limit(1)
            )
        ).scalars().first()
        assert income is not None, "No INCOME account in test DB"

        customer = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.company_id == company.id,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()

        if customer is None:
            customer = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Test & Co (50% client)",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(customer)
            await session.commit()
            await session.refresh(customer)

    # Create the quote via the API so all model defaults are applied.
    payload = {
        "customer_id": str(customer.id),
        "title": "Test Project $100 & More",
        "scope": "Supply & install — 50% of works",
        "issue_date": "2026-06-01",
        "expiry_date": "2026-06-15",
        "lines": [
            {
                "description": "Steel beam — section_A item #1",
                "quantity": "4",
                "unit_price": "0.00",
                "account_id": str(income.id),
                "section_label": "Structural Steel",
                "material": "300PLUS RHS 150x100x6",
                "length_note": "6.0m",
                "drawing_ref": "SK-001",
            },
            {
                "description": "Section A subtotal",
                "quantity": "1",
                "unit_price": "12000.00",
                "account_id": str(income.id),
                "section_label": "Structural Steel",
            },
            {
                "description": "Weld & grind — item_B 100%",
                "quantity": "8",
                "unit_price": "0.00",
                "account_id": str(income.id),
                "section_label": "Fabrication Works",
                "material": "ER70S-6",
                "length_note": "",
                "drawing_ref": "SK-002",
            },
            {
                "description": "Fabrication Works subtotal",
                "quantity": "1",
                "unit_price": "8000.00",
                "account_id": str(income.id),
                "section_label": "Fabrication Works",
            },
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        resp = await client.post("/api/v1/quotes", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_quote_pdf_returns_pdf(
    api_client: AsyncClient,
    quote_id: str,
    respx_mock: respx.MockRouter,
) -> None:
    """GET /{id}/pdf → 200 application/pdf; respx mocks latex-api."""
    import os

    import saebooks.services.latex_pdf as _svc

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE
    _svc._env = None

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(
            200,
            json={"status": "ok", "pdf_url": _FAKE_PDF_URL, "id": "q-test-1"},
        )
    )
    respx_mock.get(f"{_LATEX_API_BASE}{_FAKE_PDF_URL}").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    resp = await api_client.get(f"/api/v1/quotes/{quote_id}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    # Content-Disposition should carry the quote number/title
    cd = resp.headers.get("content-disposition", "")
    assert "SAE-2026-" in cd or "quote" in cd.lower()


@pytest.mark.asyncio
async def test_quote_pdf_404(api_client: AsyncClient) -> None:
    """Unknown quote UUID → 404."""
    resp = await api_client.get(f"/api/v1/quotes/{uuid.uuid4()}/pdf")
    assert resp.status_code == 404, resp.text
