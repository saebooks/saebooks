"""Finding 3/11 — reversing a posted taxable entry nets its tax boxes to
zero, not just its base boxes.

Both the REVERSED original and the POSTED reversal are in
REPORTABLE_STATUSES; the base already cancels via the swapped debit/credit,
and (this fix) the tax cancels too — ``journal.reverse()`` mirrors the
original's tax components onto the reversal lines and
``_aggregate_ledger_by_box`` signs a reversal entry's contribution
negative. Promoted from the critic's zzscratch reproduction.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import settings as settings_svc
from saebooks.services.reports import REPORTABLE_STATUSES
from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _aggregate_ledger_by_box,
    _parse_box_definition,
)

pytestmark = pytest.mark.postgres_only


async def test_plain_purchase_reversal_nets_tax_amount_box() -> None:
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Company(id=company_id, tenant_id=DEFAULT_TENANT_ID, name="repro", base_currency="EUR", fin_year_start_month=1, audit_mode="immutable", jurisdiction="AU"))
        await session.flush()
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")
        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Bank", account_type=AccountType.ASSET),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "gst_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="GST Paid", account_type=AccountType.ASSET),
        }
        for a in accounts.values():
            session.add(a)
        await session.flush()
        tc = TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="STD", name="Standard", rate=Decimal("10.000"), tax_system="GST", jurisdiction="AU", reporting_type="purchase_standard")
        session.add(tc)
        await session.commit()
        tc_id = tc.id
        acct_ids = {k: a.id for k, a in accounts.items()}

    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session, company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 5, 15), description="repro purchase",
            lines=[
                {"account_id": acct_ids["expense"], "debit": Decimal("1000.00"), "credit": Decimal("0"), "tax_code_id": tc_id, "gst_amount": Decimal("100.00")},
                {"account_id": acct_ids["bank"], "debit": Decimal("0"), "credit": Decimal("1100.00")},
            ],
        )
        entry = await journal_svc.post(session, entry.id, posted_by="repro")
        entry_id = entry.id

    box_def = _parse_box_definition(_BoxDefRow(
        box_code="1B", box_label="Input tax",
        aggregation="sum_tax_amount_for_codes:purchase",
        feeder_tax_codes=["purchase_standard"], display_order=1, formula=None,
    ))

    async with AsyncSessionLocal() as session:
        before = await _aggregate_ledger_by_box(
            session, [box_def], company_id=company_id, tenant_id=None,
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 31),
            statuses=REPORTABLE_STATUSES, exclude_archived=False,
        )
    assert before["1B"] == Decimal("100.00")

    async with AsyncSessionLocal() as session:
        await journal_svc.reverse(session, entry_id, posted_by="repro-reverse", tenant_id=DEFAULT_TENANT_ID)

    async with AsyncSessionLocal() as session:
        after = await _aggregate_ledger_by_box(
            session, [box_def], company_id=company_id, tenant_id=None,
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 31),
            statuses=REPORTABLE_STATUSES, exclude_archived=False,
        )
    assert after["1B"] == Decimal("0.00"), f"expected 0 after reversal, got {after['1B']}"
