"""Tests for the invoice PDF render context + company-default payment terms.

Covers (gitea #30 sub-items 2/3/4):
* _build_invoice_ctx — letterhead contact fields (phone/email/website) in the
  company block; Remit-to bank_details precedence (flagged show_on_invoice
  account wins over the company's static bank_* columns).
* POST /api/v1/invoices — a new invoice inherits payment_terms from
  Company.default_payment_terms when the payload omits it; an explicit
  per-document value always wins.

Modeled on test_credit_note_pdf.py (ctx unit tests are no-DB; API tests use
the seeded test DB).
"""
from __future__ import annotations

import uuid

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.api.v1.invoices import _build_invoice_ctx
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# _build_invoice_ctx unit tests — no DB required
# ---------------------------------------------------------------------------


class _FakeLine:
    line_no = 1
    description = "Widget & Co $100 50%_item"
    quantity = "2"
    unit_price = "50.00"
    line_total = "110.00"
    line_tax = "10.00"


class _FakeInvoice:
    def __init__(self):
        from datetime import date

        self.id = uuid.uuid4()
        self.number = "4042"
        self.issue_date = date(2026, 6, 1)
        self.due_date = date(2026, 6, 15)
        self.currency = "AUD"
        self.subtotal = "100.00"
        self.tax_total = "10.00"
        self.total = "110.00"
        self.amount_paid = "0.00"
        self.notes = "Test note"
        self.payment_terms = "Payment within 14 days"
        self.lines = [_FakeLine()]


class _FakeContact:
    name = "Acme & Partners"
    email = "billing@acme.com.au"
    phone = "0400 000 000"
    address_line1 = "1 Test St"
    address_line2 = ""
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
        # Remittance fallback columns (0168) + standing terms
        self.bank_name = "Westpac"
        self.bank_bsb = "034-193"
        self.bank_account_number = "485846"
        self.bank_account_name = "Example Pty Ltd"
        self.payment_terms_text = "Late payments accrue 2.5%/month."
        self.terms_url = "https://saebooks.com.au/terms"
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


def test_build_invoice_ctx_letterhead_fields() -> None:
    """company block carries phone/email/website for the PDF letterhead."""
    ctx = _build_invoice_ctx(_FakeInvoice(), _FakeContact(), _FakeCompany())

    assert ctx["number"] == "4042"
    assert ctx["payment_terms"] == "Payment within 14 days"
    assert ctx["payment_terms_text"] == "Late payments accrue 2.5%/month."
    assert ctx["terms_url"] == "https://saebooks.com.au/terms"
    assert ctx["company"]["name"] == "Example Pty Ltd"
    assert ctx["company"]["phone"] == "07 4000 0000"
    assert ctx["company"]["email"] == "accounts@example.com.au"
    assert ctx["company"]["website"] == "https://saebooks.com.au"
    assert ctx["contact"]["name"] == "Acme & Partners"


def test_build_invoice_ctx_bank_fallback_to_company_columns() -> None:
    """No flagged account → bank details come from companies.bank_* (0168)."""
    ctx = _build_invoice_ctx(_FakeInvoice(), _FakeContact(), _FakeCompany())

    expected = {
        "name": "Westpac",
        "bsb": "034-193",
        "account_number": "485846",
        "account_name": "Example Pty Ltd",
    }
    assert ctx["bank_details"] == expected
    # company.bank keeps the shipped 0168 template contract (same dict)
    assert ctx["company"]["bank"] == expected


def test_build_invoice_ctx_flagged_account_wins() -> None:
    """An account flagged show_on_invoice overrides company bank_* columns."""
    ctx = _build_invoice_ctx(
        _FakeInvoice(), _FakeContact(), _FakeCompany(), _FakeBankAccount()
    )
    expected = {
        "name": "CBA",
        "bsb": "063-000",
        "account_number": "12345678",
        "account_name": "SAE Engineering",
    }
    assert ctx["bank_details"] == expected
    assert ctx["company"]["bank"] == expected


# ---------------------------------------------------------------------------
# Company default payment terms — inheritance on invoice CREATE
# ---------------------------------------------------------------------------


async def _get_seed_company() -> tuple[str, int]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None, "No seed company in test DB"
        return str(company.id), company.version


async def _get_contact_id() -> str:
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        assert contact is not None, "Test DB has no contact"
        return str(contact.id)


async def _set_default_terms(
    api_client: AsyncClient, company_id: str, value: str
) -> None:
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200
    version = r.json()["version"]
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"default_payment_terms": value},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_invoice_inherits_company_default_payment_terms(
    api_client: AsyncClient,
) -> None:
    """POST without payment_terms → inherits default; explicit value wins."""
    company_id, _ = await _get_seed_company()
    contact_id = await _get_contact_id()
    default_terms = f"Net 14 days ({uuid.uuid4().hex[:6]})"

    await _set_default_terms(api_client, company_id, default_terms)
    try:
        # Omitted payment_terms → company default copied onto the document.
        r = await api_client.post(
            "/api/v1/invoices",
            json={
                "contact_id": contact_id,
                "issue_date": "2026-06-01",
                "due_date": "2026-06-15",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["payment_terms"] == default_terms

        # Explicit per-document value always wins over the default.
        r = await api_client.post(
            "/api/v1/invoices",
            json={
                "contact_id": contact_id,
                "issue_date": "2026-06-01",
                "due_date": "2026-06-15",
                "payment_terms": "Cash on delivery",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["payment_terms"] == "Cash on delivery"
    finally:
        # Clear the default so other tests see pre-test behaviour.
        await _set_default_terms(api_client, company_id, "")

    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.json()["default_payment_terms"] is None


# ---------------------------------------------------------------------------
# GET /{id}/render-context — the fact endpoint the app render service consumes
# ---------------------------------------------------------------------------


_RENDER_BASE = "http://web:8080"
_FAKE_PDF = b"%PDF-1.5 fake-invoice-pdf"


async def _create_invoice(api_client: AsyncClient, contact_id: str) -> dict:
    r = await api_client.post(
        "/api/v1/invoices",
        json={
            "contact_id": contact_id,
            "issue_date": "2026-06-01",
            "due_date": "2026-06-15",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_invoice_render_context_shape(api_client: AsyncClient) -> None:
    """GET /{id}/render-context returns {template, kind, ctx} with the fact ctx."""
    contact_id = await _get_contact_id()
    inv = await _create_invoice(api_client, contact_id)

    r = await api_client.get(f"/api/v1/invoices/{inv['id']}/render-context")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["template"] == "document"
    assert body["kind"] == "Tax Invoice"

    ctx = body["ctx"]
    # Company letterhead block (0171) — phone/email/website carried for the app.
    assert "company" in ctx
    for key in ("name", "phone", "email", "website"):
        assert key in ctx["company"], f"company.{key} missing"
    # Remit-to bank details + per-document payment terms are facts, not rendering.
    assert "bank_details" in ctx
    assert "payment_terms" in ctx
    assert ctx["number"] == inv["number"] or ctx["number"] == inv["id"][:8]


@pytest.mark.asyncio
async def test_invoice_render_context_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/invoices/{uuid.uuid4()}/render-context")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# GET /{id}/pdf — proxies the app render service (mock the render client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_pdf_proxies_render_service(
    api_client: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """/pdf posts ctx to the render service and streams the PDF bytes back."""
    contact_id = await _get_contact_id()
    inv = await _create_invoice(api_client, contact_id)

    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    r = await api_client.get(f"/api/v1/invoices/{inv['id']}/pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == _FAKE_PDF
    number = inv["number"] or inv["id"][:8]
    assert f"invoice-{number}.pdf" in r.headers.get("content-disposition", "")

    # The render service was handed the invoice facts, incl. the kind flag.
    assert route.called
    import json as _json

    posted = _json.loads(route.calls[0].request.content.decode())
    assert posted["kind"] == "Tax Invoice"


@pytest.mark.asyncio
async def test_credit_note_inherits_company_default_payment_terms(
    api_client: AsyncClient,
) -> None:
    """Credit notes inherit default_payment_terms the same way invoices do."""
    from saebooks.models.account import Account, AccountType

    company_id, _ = await _get_seed_company()
    contact_id = await _get_contact_id()
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
    assert account is not None, "Test DB has no INCOME account"

    default_terms = f"CN default terms ({uuid.uuid4().hex[:6]})"
    line = {
        "description": "Correction",
        "account_id": str(account.id),
        "quantity": "1",
        "unit_price": "10.00",
    }

    await _set_default_terms(api_client, company_id, default_terms)
    try:
        r = await api_client.post(
            "/api/v1/credit_notes",
            json={
                "contact_id": contact_id,
                "issue_date": "2026-06-01",
                "lines": [line],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["payment_terms"] == default_terms

        r = await api_client.post(
            "/api/v1/credit_notes",
            json={
                "contact_id": contact_id,
                "issue_date": "2026-06-01",
                "payment_terms": "Refund only",
                "lines": [line],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["payment_terms"] == "Refund only"
    finally:
        await _set_default_terms(api_client, company_id, "")
