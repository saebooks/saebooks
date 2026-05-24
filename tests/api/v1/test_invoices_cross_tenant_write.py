"""BKPR-1 P0 regression — cross-tenant FK injection on invoices.

audit-trail reference: edge-motor-dealer-20260427T144846Z (gap BKPR-1)

The edge-motor-dealer critic POSTed an invoice via POST /invoices/new with
foreign-tenant ``contact_id``, ``account_id``, and ``tax_code_id`` values
supplied verbatim. The session accepted the walsh-co UUIDs without any
tenant-scope check.

The fix in ``services/invoices.py`` validates every FK reference against
the caller's tenant_id before INSERT/UPDATE. This file exercises the
service layer directly (no RLS dependency) so the regression catches a
removal of the validation helpers even on the schema-owner role.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.services import invoices as svc
pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Two-tenant seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_tenant_seed() -> dict:
    """Create two tenants — edge (home) and walsh (foreign) — each with
    company + contact + income account + tax_code. Returns the IDs so
    the tests can assemble invoice payloads that mix tenants.
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict = {}

    async with AsyncSessionLocal() as session:
        for label, tax_rate in (("edge", "10.000"), ("walsh", "10.000")):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()
            contact_id = uuid.uuid4()
            account_id = uuid.uuid4()
            tax_code_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"BKPR-{label}-{suffix}",
                    slug=f"bkpr-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"BKPR-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()

            session.add(
                Contact(
                    id=contact_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    name=f"BKPR-Contact-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"BKP{suffix[:3]}{label[0].upper()}",
                    name=f"BKPR Income {label}",
                    account_type=AccountType.INCOME,
                )
            )
            session.add(
                TaxCode(
                    id=tax_code_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"BK{suffix[:3]}{label[0].upper()}",
                    name=f"BKPR GST {label}",
                    rate=Decimal(tax_rate),
                    tax_system="GST",
                    reporting_type="taxable",
                )
            )
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "contact_id": contact_id,
                "account_id": account_id,
                "tax_code_id": tax_code_id,
            }
        await session.commit()

    yield out

    # Best-effort cleanup. Invoices hold ON DELETE RESTRICT FKs onto
    # contacts and invoice_lines onto accounts/tax_codes. Delete in
    # dependency order.
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        for label in ("edge", "walsh"):
            ids = out[label]
            await session.execute(
                text(
                    "DELETE FROM invoice_lines WHERE invoice_id IN "
                    "(SELECT id FROM invoices WHERE company_id = :cid)"
                ),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM invoices WHERE company_id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tax_codes WHERE id = :id"),
                {"id": ids["tax_code_id"]},
            )
            await session.execute(
                text("DELETE FROM contacts WHERE id = :id"),
                {"id": ids["contact_id"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE id = :id"),
                {"id": ids["account_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": ids["tenant_id"]},
            )
        await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line(account_id: uuid.UUID, tax_code_id: uuid.UUID | None = None) -> dict:
    return {
        "description": "Consulting fee",
        "account_id": str(account_id),
        "tax_code_id": str(tax_code_id) if tax_code_id else None,
        "quantity": "1",
        "unit_price": "500.00",
        "discount_pct": "0",
    }


# ---------------------------------------------------------------------------
# Positive control — same-tenant FKs are accepted
# ---------------------------------------------------------------------------


async def test_api_create_same_tenant_succeeds(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]

    async with AsyncSessionLocal() as session:
        inv = await svc.api_create(
            session,
            edge["company_id"],
            edge["tenant_id"],
            actor="test:bkpr-1-positive",
            contact_id=edge["contact_id"],
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[_line(edge["account_id"], edge["tax_code_id"])],
        )

    assert inv is not None
    assert inv.tenant_id == edge["tenant_id"]
    assert inv.contact_id == edge["contact_id"]
    assert len(inv.lines) == 1


# ---------------------------------------------------------------------------
# Negative — foreign-tenant contact_id is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_contact_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_create(
                session,
                edge["company_id"],
                edge["tenant_id"],
                actor="test:bkpr-1-foreign-contact",
                contact_id=walsh["contact_id"],  # foreign-tenant!
                issue_date=date(2026, 4, 28),
                due_date=date(2026, 5, 28),
                lines=[_line(edge["account_id"], edge["tax_code_id"])],
            )
    assert "contact_company_mismatch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — foreign-tenant account_id on a line is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_account_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_create(
                session,
                edge["company_id"],
                edge["tenant_id"],
                actor="test:bkpr-1-foreign-account",
                contact_id=edge["contact_id"],
                issue_date=date(2026, 4, 28),
                due_date=date(2026, 5, 28),
                lines=[_line(walsh["account_id"], edge["tax_code_id"])],
            )
    assert "account not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — foreign-tenant tax_code_id on a line is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_tax_code_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_create(
                session,
                edge["company_id"],
                edge["tenant_id"],
                actor="test:bkpr-1-foreign-tax-code",
                contact_id=edge["contact_id"],
                issue_date=date(2026, 4, 28),
                due_date=date(2026, 5, 28),
                lines=[_line(edge["account_id"], walsh["tax_code_id"])],
            )
    assert "tax_code not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — fully random UUID (negative-control mirror)
# ---------------------------------------------------------------------------


async def test_api_create_unknown_contact_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_create(
                session,
                edge["company_id"],
                edge["tenant_id"],
                actor="test:bkpr-1-unknown-contact",
                contact_id=uuid.uuid4(),
                issue_date=date(2026, 4, 28),
                due_date=date(2026, 5, 28),
                lines=[_line(edge["account_id"], edge["tax_code_id"])],
            )
    assert "contact_company_mismatch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Update path — foreign-tenant contact_id on PATCH is rejected
# ---------------------------------------------------------------------------


async def test_api_update_foreign_contact_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        inv = await svc.api_create(
            session,
            edge["company_id"],
            edge["tenant_id"],
            actor="test:bkpr-1-update-setup",
            contact_id=edge["contact_id"],
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[_line(edge["account_id"], edge["tax_code_id"])],
        )
        inv_id = inv.id
        version = inv.version

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_update(
                session,
                inv_id,
                actor="test:bkpr-1-update-foreign",
                expected_version=version,
                contact_id=walsh["contact_id"],
            )
    assert "contact_company_mismatch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Update path — foreign-tenant account_id on a replaced line is rejected
# ---------------------------------------------------------------------------


async def test_api_update_foreign_line_account_rejected(two_tenant_seed: dict) -> None:
    edge = two_tenant_seed["edge"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        inv = await svc.api_create(
            session,
            edge["company_id"],
            edge["tenant_id"],
            actor="test:bkpr-1-update-line-setup",
            contact_id=edge["contact_id"],
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[_line(edge["account_id"], edge["tax_code_id"])],
        )
        inv_id = inv.id
        version = inv.version

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.InvoiceError) as exc:
            await svc.api_update(
                session,
                inv_id,
                actor="test:bkpr-1-update-line-foreign",
                expected_version=version,
                lines=[_line(walsh["account_id"], edge["tax_code_id"])],
            )
    assert "account not found in current tenant" in str(exc.value).lower()
