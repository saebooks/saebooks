"""Tests for the SAE Books <-> Xero shape converters.

Pure-function tests; no DB, no HTTP. Round-trip tests confirm that
mapping in both directions preserves the meaningful fields, while
status-mapping tests pin the never-demote rules.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.services.sync.xero.mappers import (
    saebooks_contact_to_xero,
    saebooks_invoice_to_xero,
    saebooks_journal_to_xero,
    xero_contact_to_saebooks,
    xero_invoice_to_saebooks,
)


# ---------------------------------------------------------------------- #
# Contact pull                                                           #
# ---------------------------------------------------------------------- #


def test_xero_contact_to_saebooks_handles_customer_only() -> None:
    row = {
        "ContactID": "C-1",
        "Name": "Acme",
        "EmailAddress": "ap@acme.example",
        "Phones": [
            {"PhoneType": "DEFAULT", "PhoneNumber": "5551234"},
            {"PhoneType": "FAX", "PhoneNumber": "0000"},
        ],
        "Addresses": [
            {
                "AddressType": "STREET",
                "AddressLine1": "1 Main St",
                "City": "Brisbane",
                "Region": "QLD",
                "PostalCode": "4000",
                "Country": "Australia",
            },
        ],
        "TaxNumber": "12 345 678 901",
        "IsCustomer": True,
        "IsSupplier": False,
        "ContactStatus": "ACTIVE",
        "UpdatedDateUTC": "/Date(1700000000000+0000)/",
    }
    out = xero_contact_to_saebooks(row)
    assert out.external_id == "C-1"
    assert out.contact_type == ContactType.CUSTOMER
    assert out.email == "ap@acme.example"
    assert out.phone == "5551234"
    assert out.abn == "12 345 678 901"
    assert out.address_line1 == "1 Main St"
    assert out.state == "QLD"
    assert out.archived is False


def test_xero_contact_both_customer_and_supplier() -> None:
    row = {
        "ContactID": "C-2",
        "Name": "Both Inc",
        "IsCustomer": True,
        "IsSupplier": True,
        "ContactStatus": "ARCHIVED",
    }
    out = xero_contact_to_saebooks(row)
    assert out.contact_type == ContactType.BOTH
    assert out.archived is True


# ---------------------------------------------------------------------- #
# Contact push                                                           #
# ---------------------------------------------------------------------- #


def _stub_contact(**overrides: object) -> Contact:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        name="Acme",
        contact_type=ContactType.CUSTOMER,
        email=None,
        phone=None,
        abn=None,
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postcode=None,
        country=None,
        archived_at=None,
        external_id=None,
        external_source=None,
    )
    base.update(overrides)
    return Contact(**base)


def test_saebooks_contact_to_xero_creates_when_no_external_id() -> None:
    c = _stub_contact(name="Acme", contact_type=ContactType.SUPPLIER)
    body = saebooks_contact_to_xero(c)
    assert "ContactID" not in body
    assert body["IsCustomer"] is False
    assert body["IsSupplier"] is True
    assert body["Name"] == "Acme"


def test_saebooks_contact_to_xero_updates_when_external_id_present() -> None:
    c = _stub_contact(
        external_id="C-1",
        external_source="xero",
        contact_type=ContactType.BOTH,
    )
    body = saebooks_contact_to_xero(c)
    assert body["ContactID"] == "C-1"
    assert body["IsCustomer"] is True
    assert body["IsSupplier"] is True


def test_saebooks_contact_to_xero_includes_address_when_any_field_set() -> None:
    c = _stub_contact(address_line1="1 Main St", city="Brisbane", state="QLD")
    body = saebooks_contact_to_xero(c)
    assert body["Addresses"][0]["AddressLine1"] == "1 Main St"
    assert body["Addresses"][0]["City"] == "Brisbane"
    assert body["Addresses"][0]["Region"] == "QLD"


# ---------------------------------------------------------------------- #
# Invoice pull                                                           #
# ---------------------------------------------------------------------- #


def test_xero_invoice_to_saebooks_status_authorised_maps_to_posted() -> None:
    row = {
        "InvoiceID": "I-1",
        "Type": "ACCREC",
        "Status": "AUTHORISED",
        "InvoiceNumber": "INV-001",
        "Contact": {"ContactID": "C-1", "Name": "Acme"},
        "Date": "2026-04-01T00:00:00",
        "DueDate": "2026-04-15T00:00:00",
        "SubTotal": "100.00",
        "TotalTax": "10.00",
        "Total": "110.00",
        "AmountPaid": "0.00",
        "CurrencyCode": "AUD",
        "CurrencyRate": "1.0",
        "LineItems": [
            {
                "Description": "Widget",
                "Quantity": "2",
                "UnitAmount": "50",
                "LineAmount": "100",
                "TaxAmount": "10",
                "AccountCode": "200",
            },
        ],
        "UpdatedDateUTC": "2026-04-02T03:04:05",
    }
    out = xero_invoice_to_saebooks(row)
    assert out.status == InvoiceStatus.POSTED
    assert out.contact_external_id == "C-1"
    assert out.subtotal == Decimal("100.00")
    assert out.issue_date == date(2026, 4, 1)
    assert len(out.lines) == 1
    assert out.lines[0].description == "Widget"


def test_xero_invoice_status_paid_also_maps_to_posted() -> None:
    row = {
        "InvoiceID": "I-2",
        "Status": "PAID",
        "Contact": {"ContactID": "C-9"},
        "Type": "ACCREC",
    }
    out = xero_invoice_to_saebooks(row)
    assert out.status == InvoiceStatus.POSTED


def test_xero_invoice_status_voided_maps_to_voided() -> None:
    out = xero_invoice_to_saebooks(
        {
            "InvoiceID": "I-3",
            "Status": "VOIDED",
            "Type": "ACCREC",
            "Contact": {},
        }
    )
    assert out.status == InvoiceStatus.VOIDED


# ---------------------------------------------------------------------- #
# Invoice push                                                           #
# ---------------------------------------------------------------------- #


def _stub_invoice(**overrides: object) -> Invoice:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        number="INV-001",
        issue_date=date(2026, 4, 1),
        due_date=date(2026, 4, 15),
        status=InvoiceStatus.POSTED,
        currency="AUD",
        fx_rate=Decimal("1.0"),
        external_id=None,
        external_source=None,
        version=1,
    )
    base.update(overrides)
    return Invoice(**base)


def _stub_line(**overrides: object) -> InvoiceLine:
    base = dict(
        invoice_id=uuid.uuid4(),
        line_no=1,
        description="Widget",
        account_id=uuid.uuid4(),
        quantity=Decimal("2"),
        unit_price=Decimal("50"),
        discount_pct=Decimal("0"),
    )
    base.update(overrides)
    return InvoiceLine(**base)


def test_saebooks_invoice_to_xero_creates_when_no_external_id() -> None:
    invoice = _stub_invoice(number="INV-77")
    body = saebooks_invoice_to_xero(
        invoice,
        lines=[_stub_line()],
        contact_external_id="C-9",
    )
    assert "InvoiceID" not in body
    assert body["Type"] == "ACCREC"
    assert body["Status"] == "AUTHORISED"  # POSTED -> AUTHORISED
    assert body["InvoiceNumber"] == "INV-77"
    assert body["Contact"] == {"ContactID": "C-9"}
    assert body["LineItems"][0]["Description"] == "Widget"


def test_saebooks_invoice_to_xero_updates_when_external_id_present() -> None:
    invoice = _stub_invoice(external_id="I-1", external_source="xero")
    body = saebooks_invoice_to_xero(
        invoice,
        lines=[_stub_line()],
        contact_external_id="C-9",
    )
    assert body["InvoiceID"] == "I-1"


# ---------------------------------------------------------------------- #
# Manual journal                                                         #
# ---------------------------------------------------------------------- #


def test_saebooks_journal_to_xero_signs_lines() -> None:
    body = saebooks_journal_to_xero(
        narration="Test journal",
        journal_date=date(2026, 4, 30),
        lines=[
            {"account_code": "200", "description": "Income", "amount": Decimal("100")},
            {"account_code": "300", "description": "Bank", "amount": Decimal("-100")},
        ],
    )
    assert body["Narration"] == "Test journal"
    assert body["Date"] == "2026-04-30"
    assert body["Status"] == "POSTED"
    lines = body["JournalLines"]
    assert lines[0]["LineAmount"] == "100"
    assert lines[1]["LineAmount"] == "-100"
