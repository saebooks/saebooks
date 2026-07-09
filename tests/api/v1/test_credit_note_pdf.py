"""Tests for GET /api/v1/credit_notes/{id}/pdf — LaTeX engine.

Two tests:
1. test_credit_note_pdf_returns_pdf  — 200 application/pdf; ctx builder maps key fields.
2. test_credit_note_pdf_404          — missing CN → 404.

latex-api is mocked with respx; the test DB is seeded with a real CN.
"""
from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.api.v1.credit_notes import _build_credit_note_ctx
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact

pytestmark = pytest.mark.postgres_only

_FAKE_PDF = b"%PDF-1.5 fake-cn-pdf"
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
async def cn_id() -> str:
    """Create a minimal credit note in the test DB and return its UUID string."""
    from httpx import ASGITransport, AsyncClient

    token = current_token()
    async with AsyncSessionLocal() as session:
        account = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert account is not None, "Test DB has no INCOME account"
    assert contact is not None, "Test DB has no contact"

    payload = {
        "contact_id": str(contact.id),
        "issue_date": "2026-04-15",
        "reason": "Test & Co. correction 50%",
        "notes": "Unit test CN",
        "lines": [
            {
                "line_no": 1,
                "description": "Widget_return $100 & service fee",
                "account_id": str(account.id),
                "quantity": "2",
                "unit_price": "50.00",
                "line_subtotal": "100.00",
                "line_tax": "10.00",
                "line_total": "110.00",
            }
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        resp = await client.post("/api/v1/credit_notes", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_credit_note_pdf_returns_pdf(
    api_client: AsyncClient,
    cn_id: str,
    respx_mock: respx.MockRouter,
) -> None:
    """GET /{id}/pdf → 200 application/pdf; the render service is mocked."""
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    resp = await api_client.get(f"/api/v1/credit_notes/{cn_id}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    assert "credit-note-" in resp.headers.get("content-disposition", "")

    # The credit-note facts (incl. kind) were handed to the render service.
    assert route.called
    import json as _json

    posted = _json.loads(route.calls[0].request.content.decode())
    assert posted["kind"] == "Credit Note"


@pytest.mark.asyncio
async def test_credit_note_render_context(api_client: AsyncClient, cn_id: str) -> None:
    """GET /{id}/render-context → {template, kind, ctx} with the CN fact ctx."""
    resp = await api_client.get(f"/api/v1/credit_notes/{cn_id}/render-context")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["template"] == "document"
    assert body["kind"] == "Credit Note"

    ctx = body["ctx"]
    assert ctx["kind"] == "Credit Note"  # builder sets kind inside ctx
    assert "company" in ctx
    for key in ("name", "phone", "email", "website"):
        assert key in ctx["company"], f"company.{key} missing"
    assert "bank_details" in ctx
    assert "payment_terms" in ctx


@pytest.mark.asyncio
async def test_credit_note_pdf_404(api_client: AsyncClient) -> None:
    """Unknown credit note UUID → 404."""
    import uuid

    resp = await api_client.get(f"/api/v1/credit_notes/{uuid.uuid4()}/pdf")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# _build_credit_note_ctx unit test — no DB required
# ---------------------------------------------------------------------------


class _FakeLine:
    def __init__(self):
        self.line_no = 1
        self.description = "Widget & Co $100 50%_item"
        self.quantity = "2"
        self.unit_price = "50.00"
        self.line_total = "110.00"
        self.line_tax = "10.00"


class _FakeCN:
    def __init__(self):
        import uuid
        self.id = uuid.uuid4()
        self.number = "CN-999"
        self.issue_date = None
        self.subtotal = "100.00"
        self.tax_total = "10.00"
        self.total = "110.00"
        self.amount_allocated = "0.00"
        self.notes = "Test note"
        self.payment_terms = "Strictly 14 days"
        self.lines = [_FakeLine()]

    @property
    def issue_date(self):
        from datetime import date
        return date(2026, 4, 15)

    @issue_date.setter
    def issue_date(self, v):
        pass


class _FakeContact:
    name = "Acme & Partners"
    email = "billing@acme.com.au"
    phone = "0400 000 000"
    address_line1 = "1 Test St"
    city = "Brisbane"
    state = "QLD"
    postcode = "4000"
    country = "Australia"


class _FakeCompany:
    def __init__(self):
        self.legal_name = "Example Pty Ltd"
        self.name = "Example Pty Ltd"
        self.abn = "12 345 678 901"
        # Letterhead contact details (0171)
        self.phone = "07 4000 0000"
        self.email = "accounts@example.com.au"
        self.website = "https://saebooks.com.au"
        # Remittance fallback columns (0168)
        self.bank_name = "Westpac"
        self.bank_bsb = "034-193"
        self.bank_account_number = "485846"
        self.bank_account_name = "Example Pty Ltd"
        self.address = {
            "address_line1": "123 Workshop Rd",
            "city": "Archerfield",
            "state": "QLD",
            "postcode": "4108",
            "country": "Australia",
        }


class _FakeBankAccount:
    """Account row flagged show_on_invoice (ABA fields)."""

    name = "Operating Account"
    bsb = "063-000"
    bank_account_number = "12345678"
    bank_account_title = "SAE Engineering"
    bank_abbreviation = "CBA"


def test_build_credit_note_ctx_fields() -> None:
    """_build_credit_note_ctx maps all required fields correctly."""
    cn = _FakeCN()
    contact = _FakeContact()
    company = _FakeCompany()

    ctx = _build_credit_note_ctx(cn, contact, company)

    assert ctx["kind"] == "Credit Note"
    assert ctx["number"] == "CN-999"
    assert ctx["issue_date"] == "2026-04-15"
    assert ctx["due_date"] == ""
    assert ctx["currency"] == "AUD"
    assert ctx["subtotal"] == "100.00"
    assert ctx["tax_total"] == "10.00"
    assert ctx["total"] == "110.00"
    assert ctx["amount_paid"] == "0.00"
    assert ctx["company"]["abn"] == "12 345 678 901"
    assert ctx["contact"]["name"] == "Acme & Partners"
    assert len(ctx["lines"]) == 1
    line = ctx["lines"][0]
    assert line["description"] == "Widget & Co $100 50%_item"
    assert line["line_tax"] == "10.00"
    # 0171 — per-document payment terms + letterhead contact fields
    assert ctx["payment_terms"] == "Strictly 14 days"
    assert ctx["company"]["phone"] == "07 4000 0000"
    assert ctx["company"]["email"] == "accounts@example.com.au"
    assert ctx["company"]["website"] == "https://saebooks.com.au"
    # No flagged bank account → falls back to the company's bank_* columns
    assert ctx["bank_details"] == {
        "name": "Westpac",
        "bsb": "034-193",
        "account_number": "485846",
        "account_name": "Example Pty Ltd",
    }


def test_build_credit_note_ctx_flagged_account_wins() -> None:
    """A bank account flagged show_on_invoice overrides company bank_* columns."""
    ctx = _build_credit_note_ctx(
        _FakeCN(), _FakeContact(), _FakeCompany(), _FakeBankAccount()
    )
    assert ctx["bank_details"] == {
        "name": "CBA",
        "bsb": "063-000",
        "account_number": "12345678",
        "account_name": "SAE Engineering",
    }
