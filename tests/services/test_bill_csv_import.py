"""Safety tests for the bill-CSV importer.

Pins: valid rows → DRAFT bills (never posted); rows whose supplier /
account / tax code don't resolve are REJECTED with errors, not guessed
and not created.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import contacts as contacts_svc
from saebooks.services.imports import bill_csv as bill_csv_svc

pytestmark = pytest.mark.postgres_only

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SUPPLIER = "CSV Importer Test Supplier"


async def _ctx():
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at).limit(1)
        )).scalars().first()
        acct = (await s.execute(
            select(Account).where(
                Account.company_id == co.id,
                Account.account_type == AccountType.EXPENSE,
                Account.is_header.is_(False),
            ).limit(1)
        )).scalars().first()
        tc = (await s.execute(
            select(TaxCode).where(TaxCode.company_id == co.id).limit(1)
        )).scalars().first()
        existing = (await s.execute(
            select(Contact).where(Contact.company_id == co.id, Contact.name == _SUPPLIER).limit(1)
        )).scalars().first()
        if existing is None:
            await contacts_svc.create(
                s, co.id, tenant_id=_TENANT, name=_SUPPLIER,
                contact_type=ContactType.SUPPLIER,
            )
        return co.id, acct.code, tc.code


@pytest.mark.asyncio
async def test_bill_csv_creates_drafts_and_rejects_bad_rows() -> None:
    company_id, acct_code, tax_code = await _ctx()
    csv = (
        "supplier,date,account,amount,tax_code,reference,description\n"
        f"{_SUPPLIER},2025-05-10,{acct_code},100.00,{tax_code},INV-CSV-1,Widgets\n"
        f"No Such Supplier Ltd,2025-05-10,{acct_code},50.00,{tax_code},INV-CSV-2,Bad supplier\n"
        f"{_SUPPLIER},2025-05-10,9-9999,50.00,{tax_code},INV-CSV-3,Bad account\n"
        f"{_SUPPLIER},not-a-date,{acct_code},50.00,{tax_code},INV-CSV-4,Bad date\n"
    )
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        result = await bill_csv_svc.commit_bill_csv(
            s, company_id=company_id, tenant_id=_TENANT, raw=csv
        )

    assert result["created"] == 1, result
    assert result["skipped"] == 3, result
    reasons = " ".join(e["error"] for e in result["errors"]).lower()
    assert "supplier not found" in reasons
    assert "account code not found" in reasons
    assert "date" in reasons

    # The one created bill is a DRAFT, never posted, with the line total.
    bill_id = uuid.UUID(result["bill_ids"][0])
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        bill = (await s.execute(select(Bill).where(Bill.id == bill_id))).scalars().first()
        assert bill is not None
        assert bill.status == BillStatus.DRAFT
        assert bill.posted_at is None
        assert bill.supplier_reference == "INV-CSV-1"
        assert bill.subtotal == 100  # one line @ 100


@pytest.mark.asyncio
async def test_bill_csv_rejects_csv_missing_required_columns() -> None:
    company_id, _a, _t = await _ctx()
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        with pytest.raises(bill_csv_svc.BillCsvError):
            await bill_csv_svc.commit_bill_csv(
                s, company_id=company_id, tenant_id=_TENANT,
                raw="foo,bar\n1,2\n",
            )
