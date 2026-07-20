"""``services.einvoice.buyer_requirement`` — pure tests + DB-integration
tests proving the surfacing actually fires on invoice creation."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.contact import Contact, ContactType
from saebooks.services import invoices as invoices_svc
from saebooks.services.einvoice import buyer_requirement as br
from tests.services.einvoice.test_generator_golden import (
    _account_id,
    _naidis_company,
    _tax_code_id,
)


def _contact(**overrides) -> Contact:
    defaults = dict(
        company_id=uuid.uuid4(), name="Test Buyer", contact_type=ContactType.CUSTOMER,
    )
    defaults.update(overrides)
    return Contact(**defaults)


def test_not_required_by_default() -> None:
    c = _contact()
    assert br.einvoice_required(c) is False
    assert br.review_note_for_new_invoice(c) is None


def test_required_when_flagged() -> None:
    c = _contact(e_invoice_recipient=True)
    assert br.einvoice_required(c) is True
    note = br.review_note_for_new_invoice(c)
    assert note is not None
    assert "2025-07-01" in note
    assert "No Peppol routing address on file" in note


def test_note_includes_peppol_address_when_present() -> None:
    c = _contact(e_invoice_recipient=True, peppol_participant_id="0191:10137025")
    note = br.describe_requirement(c)
    assert "0191:10137025" in note
    assert "No Peppol routing address" not in note


def test_review_note_none_for_missing_contact() -> None:
    assert br.review_note_for_new_invoice(None) is None


# --------------------------------------------------------------------------- #
# DB integration — proves services.invoices actually wires this in.
# --------------------------------------------------------------------------- #


async def _flagged_contact(company_id: uuid.UUID, *, peppol_id: str | None = None) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name="Flagged Buyer AS", contact_type=ContactType.CUSTOMER,
            registration_number="12345678", country="Estonia",
            e_invoice_recipient=True, peppol_participant_id=peppol_id,
        )
        session.add(c)
        await session.commit()
        return c.id


@pytest.mark.postgres_only
async def test_create_draft_flags_invoice_for_recipient_contact() -> None:
    company_id = await _naidis_company()
    contact_id = await _flagged_contact(company_id, peppol_id="0191:12345678")
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Flagged buyer line", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )

    assert inv.flagged_for_review is True
    assert inv.review_note is not None
    assert "2025-07-01" in inv.review_note
    assert "0191:12345678" in inv.review_note


@pytest.mark.postgres_only
async def test_create_draft_does_not_flag_ordinary_contact() -> None:
    company_id = await _naidis_company()
    async with AsyncSessionLocal() as session:
        c = Contact(company_id=company_id, name="Ordinary Buyer", contact_type=ContactType.CUSTOMER)
        session.add(c)
        await session.commit()
        contact_id = c.id
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Ordinary buyer line", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )

    assert inv.flagged_for_review is False
    assert inv.review_note is None


@pytest.mark.postgres_only
async def test_api_create_also_flags_recipient_contact() -> None:
    """Same surfacing must fire on the real API-facing entrypoint
    (services.invoices.api_create), not just the legacy create_draft."""
    from saebooks.api.v1.auth import DEFAULT_TENANT_ID

    company_id = await _naidis_company()
    contact_id = await _flagged_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.api_create(
            session, company_id, DEFAULT_TENANT_ID, "pytest-einvoice-buyer-flag",
            contact_id=contact_id, issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25),
            currency="EUR",
            lines=[{
                "description": "API-create flagged buyer line", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )

    assert inv.flagged_for_review is True
    assert inv.review_note is not None
    assert "2025-07-01" in inv.review_note


@pytest.mark.postgres_only
async def test_flagged_invoice_is_listable_via_flagged_filter() -> None:
    """The 'surfacing' is the review queue — prove list_active(flagged=True)
    actually finds it, the real-world path a books-review UI would use."""
    company_id = await _naidis_company()
    contact_id = await _flagged_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Listable flagged line", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )

    from saebooks.api.v1.auth import DEFAULT_TENANT_ID

    async with AsyncSessionLocal() as session:
        flagged, total = await invoices_svc.list_active(
            session, company_id, DEFAULT_TENANT_ID, flagged=True
        )
    assert total >= 1
    assert any(i.id == inv.id for i in flagged)


@pytest.mark.postgres_only
async def test_contact_model_defaults_are_false_and_null() -> None:
    """Regression guard for migration 0197's own additive contract — every
    existing/new contact defaults to not-required, no routing address."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        from saebooks.models.company import Company

        session.add(Company(id=company_id, name="Plain AU Co"))
        c = Contact(company_id=company_id, name="Plain Contact", contact_type=ContactType.CUSTOMER)
        session.add(c)
        await session.commit()
        contact_id = c.id

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one()
        assert row.e_invoice_recipient is False
        assert row.peppol_participant_id is None
