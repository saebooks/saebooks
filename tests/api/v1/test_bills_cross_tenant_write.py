"""Cross-tenant FK injection regression — bills.

POST /bills/new previously accepted ``contact_id``, ``account_id`` and
``tax_code_id`` values from a different tenant verbatim — the session did
not check that referenced FKs belonged to the caller's tenant, and the
line item's GST was computed at the foreign tenant's tax rate.
``services/bills.py`` now validates every FK reference against the
caller's tenant_id before INSERT/UPDATE; this file exercises the service
layer directly (no RLS dependency) so the regression catches a removal
of the validation helpers even on the schema-owner role.
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
from saebooks.services import bills as svc


# ---------------------------------------------------------------------------
# Two-tenant seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_tenant_seed() -> dict:
    """Create two tenants — apex (home) and walsh (foreign) — each with
    company + contact + expense account + tax_code. Returns the IDs so
    the tests can assemble bill payloads that mix tenants.
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict = {}

    async with AsyncSessionLocal() as session:
        for label, tax_rate in (("apex", "7.000"), ("walsh", "10.000")):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()
            contact_id = uuid.uuid4()
            account_id = uuid.uuid4()
            tax_code_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Test-{label}-{suffix}",
                    slug=f"civl-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"Test-{label}-{suffix}",
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
                    name=f"Test-Contact-{label}",
                    contact_type=ContactType.SUPPLIER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"CIV{suffix[:3]}{label[0].upper()}",
                    name=f"Test Expense {label}",
                    account_type=AccountType.EXPENSE,
                )
            )
            session.add(
                TaxCode(
                    id=tax_code_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"CV{suffix[:3]}{label[0].upper()}",
                    name=f"Test GST {label}",
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

    # Best-effort cleanup. Bills hold ON DELETE RESTRICT FKs onto contacts
    # and bill_lines onto accounts/tax_codes, so any draft bills created
    # by the positive-control tests must be removed before we can drop
    # the seed rows. Companies CASCADE clears child rows on most
    # subordinate tables but not bill_lines/bills (RESTRICT). Hence the
    # explicit delete order below.
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        for label in ("apex", "walsh"):
            ids = out[label]
            await session.execute(
                text("DELETE FROM bill_lines WHERE bill_id IN "
                     "(SELECT id FROM bills WHERE company_id = :cid)"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM bills WHERE company_id = :cid"),
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
        "description": "Sub-contractor charge",
        "account_id": str(account_id),
        "tax_code_id": str(tax_code_id) if tax_code_id else None,
        "quantity": "1",
        "unit_price": "1000.00",
        "discount_pct": "0",
    }


# ---------------------------------------------------------------------------
# Positive control — same-tenant FKs are accepted
# ---------------------------------------------------------------------------


async def test_api_create_same_tenant_succeeds(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]

    async with AsyncSessionLocal() as session:
        bill = await svc.api_create(
            session,
            apex["company_id"],
            apex["tenant_id"],
            actor="test:civl-1-positive",
            contact_id=apex["contact_id"],
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 5, 1),
            lines=[_line(apex["account_id"], apex["tax_code_id"])],
        )

    assert bill is not None
    assert bill.tenant_id == apex["tenant_id"]
    assert bill.contact_id == apex["contact_id"]
    assert len(bill.lines) == 1


# ---------------------------------------------------------------------------
# Negative — foreign-tenant contact_id is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_contact_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:civl-1-foreign-contact",
                contact_id=walsh["contact_id"],  # foreign-tenant!
                issue_date=date(2026, 4, 1),
                due_date=date(2026, 5, 1),
                lines=[_line(apex["account_id"], apex["tax_code_id"])],
            )
    assert "contact not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — foreign-tenant account_id on a line is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_account_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:civl-1-foreign-account",
                contact_id=apex["contact_id"],
                issue_date=date(2026, 4, 1),
                due_date=date(2026, 5, 1),
                lines=[_line(walsh["account_id"], apex["tax_code_id"])],
            )
    assert "account not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — foreign-tenant tax_code_id on a line is rejected
# ---------------------------------------------------------------------------


async def test_api_create_foreign_tax_code_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:civl-1-foreign-tax-code",
                contact_id=apex["contact_id"],
                issue_date=date(2026, 4, 1),
                due_date=date(2026, 5, 1),
                lines=[_line(apex["account_id"], walsh["tax_code_id"])],
            )
    assert "tax_code not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — fully random UUID for contact (negative control mirror) — same
# error contract as the cross-tenant case so the API surface is consistent.
# ---------------------------------------------------------------------------


async def test_api_create_unknown_contact_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:civl-1-unknown-contact",
                contact_id=uuid.uuid4(),
                issue_date=date(2026, 4, 1),
                due_date=date(2026, 5, 1),
                lines=[_line(apex["account_id"], apex["tax_code_id"])],
            )
    assert "contact not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Update path — foreign-tenant contact_id on PATCH is rejected
# ---------------------------------------------------------------------------


async def test_api_update_foreign_contact_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        bill = await svc.api_create(
            session,
            apex["company_id"],
            apex["tenant_id"],
            actor="test:civl-1-update-setup",
            contact_id=apex["contact_id"],
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 5, 1),
            lines=[_line(apex["account_id"], apex["tax_code_id"])],
        )
        bill_id = bill.id
        version = bill.version

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_update(
                session,
                bill_id,
                actor="test:civl-1-update-foreign",
                expected_version=version,
                contact_id=walsh["contact_id"],
            )
    assert "contact not found in current tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Update path — foreign-tenant account_id on a replaced line is rejected
# ---------------------------------------------------------------------------


async def test_api_update_foreign_line_account_rejected(two_tenant_seed: dict) -> None:
    apex = two_tenant_seed["apex"]
    walsh = two_tenant_seed["walsh"]

    async with AsyncSessionLocal() as session:
        bill = await svc.api_create(
            session,
            apex["company_id"],
            apex["tenant_id"],
            actor="test:civl-1-update-line-setup",
            contact_id=apex["contact_id"],
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 5, 1),
            lines=[_line(apex["account_id"], apex["tax_code_id"])],
        )
        bill_id = bill.id
        version = bill.version

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError) as exc:
            await svc.api_update(
                session,
                bill_id,
                actor="test:civl-1-update-line-foreign",
                expected_version=version,
                lines=[_line(walsh["account_id"], apex["tax_code_id"])],
            )
    assert "account not found in current tenant" in str(exc.value).lower()
