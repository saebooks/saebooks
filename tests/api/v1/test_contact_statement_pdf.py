"""Tests for GET /api/v1/contacts/{id}/statement.pdf — LaTeX engine.

Tests:
1. test_contact_statement_pdf_returns_pdf — 200 application/pdf; mocked latex-api.
2. test_contact_statement_pdf_no_contact — unknown contact_id → 404.

latex-api is mocked with respx; does not hit the live service.
All tests are postgres_only (need a seeded DB with a company + contact).
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
from saebooks.models.company import Company
from saebooks.models.contact import Contact

pytestmark = pytest.mark.postgres_only

_FAKE_PDF = b"%PDF-1.5 fake-contact-statement-pdf"
_FAKE_PDF_URL = "/files/contact-stmt-test.pdf"
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
async def contact_id() -> str:
    """Return the UUID string of an existing contact in the test DB."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if company is None:
            pytest.skip("No active company in default tenant")

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.company_id == company.id,
                    Contact.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if contact is None:
            pytest.skip("No contact in default tenant company")

    return str(contact.id)


@pytest.mark.asyncio
async def test_contact_statement_pdf_returns_pdf(
    api_client: AsyncClient,
    contact_id: str,
    respx_mock: respx.MockRouter,
) -> None:
    """GET /api/v1/contacts/{id}/statement.pdf → 200 application/pdf.

    latex-api is mocked; context assembly from DB + render_statement_pdf
    (async LaTeX path) is exercised end-to-end.
    """
    import os

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE

    import saebooks.services.latex_pdf as _svc

    _svc._env = None  # invalidate cached env so it picks up test URL

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(
            200,
            json={"status": "ok", "pdf_url": _FAKE_PDF_URL, "id": "stmt-test"},
        )
    )
    respx_mock.get(f"{_LATEX_API_BASE}{_FAKE_PDF_URL}").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    resp = await api_client.get(
        f"/api/v1/contacts/{contact_id}/statement.pdf",
        params={"from": "2020-07-01", "to": "2026-06-30"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF


@pytest.mark.asyncio
async def test_contact_statement_pdf_no_contact(
    api_client: AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """Unknown contact_id → 404."""
    import os

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE

    # respx should never be called — the 404 path is before render_statement_pdf.
    resp = await api_client.get(
        f"/api/v1/contacts/{uuid.uuid4()}/statement.pdf",
        params={"from": "2020-07-01", "to": "2026-06-30"},
    )

    assert resp.status_code == 404, resp.text
