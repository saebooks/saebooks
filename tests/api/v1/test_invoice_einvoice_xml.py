"""Contract tests for GET /api/v1/invoices/{id}/einvoice.xml.

Exercises the HTTP surface of ``services.einvoice.generator`` exposed by the
JSON router. Seeding of the EE company + posted EUR invoice reuses the
service-level golden test's helpers (``tests.services.einvoice
.test_generator_golden``) so the fixture stays a single source of truth, and
the emitted XML is validated against the SAME real UBL 2.1 XSD the service
test uses.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from lxml import etree

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.tenant import Tenant
from saebooks.services import business_identifiers
from saebooks.services.einvoice import mapping as m
from tests.services.einvoice._ubl_validation import validate_ubl_invoice
from tests.services.einvoice.test_generator_golden import (
    _naidis_company,
    _ostja_contact,
    _post_standard_invoice,
)

pytestmark = pytest.mark.postgres_only

_NS = {"cac": m.NS_CAC, "cbc": m.NS_CBC}


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def test_einvoice_xml_round_trip_for_posted_ee_invoice(
    api_client: AsyncClient,
) -> None:
    company_id = await _naidis_company()
    # Record the seller VAT number (BT-31) — EN 16931 BR-S-02 requires it on a
    # Standard-rated line. The route resolves it from the ``ee_vat`` business
    # identifier, the same registry the regcode comes from.
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        await business_identifiers.upsert(
            session, company_id, "ee_vat", "EE101370251", tenant_id=company.tenant_id,
        )
        await session.commit()
    contact_id = await _ostja_contact(company_id)
    invoice_id = await _post_standard_invoice(company_id, contact_id)

    r = await api_client.get(
        f"/api/v1/invoices/{invoice_id}/einvoice.xml",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    disposition = r.headers["content-disposition"]
    assert 'filename="invoice-' in disposition and disposition.endswith('.xml"')

    xml_bytes = r.content
    validate_ubl_invoice(xml_bytes)  # same real UBL 2.1 XSD as the service golden test
    root = etree.fromstring(xml_bytes)
    assert root.findtext("cbc:DocumentCurrencyCode", namespaces=_NS) == "EUR"
    assert (
        root.findtext(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID",
            namespaces=_NS,
        )
        == "10137025"
    )
    # The route's novel logic: seller VAT (BT-31) resolved from the ee_vat
    # business identifier must reach PartyTaxScheme/CompanyID, not just be
    # "present enough" to clear BR-S-02.
    assert (
        root.findtext(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
            namespaces=_NS,
        )
        == "EE101370251"
    )
    payable = Decimal(
        root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=_NS)
    )
    assert payable == Decimal("1240.00")  # 1000 net + 24% VAT


async def test_einvoice_xml_foreign_tenant_invoice_404(api_client: AsyncClient) -> None:
    """An invoice belonging to a different tenant is RLS-invisible to the
    caller and must 404, never leak or 500."""
    other_invoice_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        tenant_id = uuid.uuid4()
        company_id = uuid.uuid4()
        contact_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, name=f"foreign-{tenant_id.hex[:8]}", slug=f"foreign-{tenant_id.hex[:8]}"))
        await session.flush()
        session.add(Company(id=company_id, tenant_id=tenant_id, name="Foreign OU", base_currency="EUR", jurisdiction="EE"))
        await session.flush()
        session.add(Contact(id=contact_id, tenant_id=tenant_id, company_id=company_id, name="Foreign Buyer", contact_type=ContactType.CUSTOMER))
        await session.flush()
        session.add(Invoice(
            id=other_invoice_id, tenant_id=tenant_id, company_id=company_id,
            contact_id=contact_id, number="FGN-1", issue_date=date(2026, 7, 11),
            due_date=date(2026, 7, 25), status=InvoiceStatus.POSTED, currency="EUR",
        ))
        await session.commit()

    r = await api_client.get(f"/api/v1/invoices/{other_invoice_id}/einvoice.xml")
    assert r.status_code == 404, r.text


async def test_einvoice_xml_draft_invoice_422(api_client: AsyncClient) -> None:
    """A DRAFT invoice cannot be e-invoiced; the generator's typed refusal
    surfaces as 422 with its own message, not a 500."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)

    async with AsyncSessionLocal() as session:
        from saebooks.models.account import Account
        from saebooks.models.tax_code import TaxCode
        from sqlalchemy import select
        from saebooks.services import invoices as invoices_svc

        income = (await session.execute(
            select(Account.id).where(Account.company_id == company_id, Account.code == "4-1000")
        )).scalar_one()
        tax_code_id = (await session.execute(
            select(TaxCode.id).where(TaxCode.company_id == company_id, TaxCode.reporting_type == "standard")
        )).scalars().first()
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Draft only", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )
        draft_id = inv.id

    r = await api_client.get(
        f"/api/v1/invoices/{draft_id}/einvoice.xml",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 422, r.text
    assert "POSTED" in r.json()["detail"]
