"""Finding 2/8 — an EU-acquisition reverse-charge bill posts economically
correct GL through ``services.bills.post_bill`` (was a hard block).

Asserts, from ONE posted bill:
  * the journal balances (Dr == Cr);
  * Accounts Payable is credited the NET invoice only (no VAT — the
    foreign supplier charged none);
  * the self-assessed OUTPUT VAT is booked as a liability on the
    RC-payable account;
  * the deductible INPUT VAT is booked as a receivable (GST Paid),
    auto-posted off the expense line's gst_amount — the two net to zero;
  * the KMD role-keyed boxes (1_RC output base, 5_RC input VAT) and the
    informative purchase box 6 read the posted line.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bills_svc
from saebooks.services import settings as settings_svc
from saebooks.services.reports import REPORTABLE_STATUSES
from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _aggregate_ledger_by_box,
    _parse_box_definition,
)

pytestmark = pytest.mark.postgres_only


async def _make_ee_rc_company() -> tuple[uuid.UUID, dict[str, uuid.UUID], uuid.UUID, uuid.UUID]:
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id, tenant_id=DEFAULT_TENANT_ID,
                name=f"RC Bill {company_id.hex[:8]}", base_currency="EUR",
                fin_year_start_month=1, audit_mode="immutable", jurisdiction="EE",
            )
        )
        await session.flush()
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")
        await settings_svc.set(
            session, bills_svc.RC_PAYABLE_ACCOUNT_SETTING_KEY, "2-1350"
        )
        accounts = {
            "ap": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "gst_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="GST Paid", account_type=AccountType.ASSET),
            "gst_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="GST Collected", account_type=AccountType.LIABILITY),
            "rc_payable": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1350", name="VAT self-assessed (reverse charge)", account_type=AccountType.LIABILITY),
        }
        for a in accounts.values():
            session.add(a)
        rc_tc = TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="RC-EUACQ", name="EE reverse charge — EU acquisition of goods (24%)", rate=Decimal("24.000"), tax_system="VAT", jurisdiction="EE", reporting_type="rc_eu_acq_goods")
        session.add(rc_tc)
        contact = Contact(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, name="EU Publisher OÜ", contact_type=ContactType.SUPPLIER)
        session.add(contact)
        await session.flush()
        acct_ids = {k: a.id for k, a in accounts.items()}
        rc_tc_id = rc_tc.id
        contact_id = contact.id
        await session.commit()
    return company_id, acct_ids, rc_tc_id, contact_id


async def test_reverse_charge_bill_posts_balanced_correct_gl_and_kmd_boxes() -> None:
    company_id, accounts, rc_tc_id, contact_id = await _make_ee_rc_company()
    net = Decimal("4000.00")
    vat = Decimal("960.00")  # 24% self-assessed

    async with AsyncSessionLocal() as session:
        bill = await bills_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 5, 15), due_date=date(2026, 6, 15),
            currency="EUR",
            lines=[{
                "description": "EU books acquisition",
                "account_id": accounts["expense"],
                "tax_code_id": rc_tc_id,
                "quantity": Decimal("1"),
                "unit_price": net,
                "discount_pct": Decimal("0"),
            }],
        )
        bill_id = bill.id

    async with AsyncSessionLocal() as session:
        await bills_svc.post_bill(session, bill_id, posted_by="pytest-rc-bill")

    # --- GL correctness ---------------------------------------------------
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(JournalLine.account_id, JournalLine.debit, JournalLine.credit)
                .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                .where(JournalEntry.company_id == company_id,
                       JournalEntry.status == EntryStatus.POSTED)
            )
        ).all()
    by_acct: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    tot_dr = tot_cr = Decimal("0")
    for aid, dr, cr in rows:
        dr = dr or Decimal("0"); cr = cr or Decimal("0")
        d, c = by_acct.get(aid, (Decimal("0"), Decimal("0")))
        by_acct[aid] = (d + dr, c + cr)
        tot_dr += dr; tot_cr += cr

    assert tot_dr == tot_cr, f"journal must balance: Dr {tot_dr} != Cr {tot_cr}"
    assert by_acct[accounts["ap"]] == (Decimal("0.00"), net), "AP = net invoice only"
    assert by_acct[accounts["rc_payable"]] == (Decimal("0.00"), vat), "output VAT liability booked"
    assert by_acct[accounts["gst_paid"]] == (vat, Decimal("0.00")), "input VAT receivable booked"
    assert by_acct[accounts["expense"]] == (net, Decimal("0.00"))

    # --- KMD boxes from the one posted bill -------------------------------
    box_defs = [
        _parse_box_definition(_BoxDefRow("1_RC", "RC output base", "sum_taxable_for_codes:output:gst_exclusive", ["rc_eu_acq_goods", "rc_eu_acq_services"], 102)),
        _parse_box_definition(_BoxDefRow("5_RC", "RC input VAT", "sum_tax_amount_for_codes:input", ["rc_eu_acq_goods", "rc_eu_acq_services"], 142)),
        _parse_box_definition(_BoxDefRow("6", "acq (info)", "sum_taxable_for_codes:purchase:gst_exclusive", ["rc_eu_acq_goods", "rc_eu_acq_services"], 19)),
    ]
    async with AsyncSessionLocal() as session:
        amounts = await _aggregate_ledger_by_box(
            session, box_defs, company_id=company_id, tenant_id=None,
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 31),
            statuses=REPORTABLE_STATUSES, exclude_archived=False,
        )
    assert amounts["1_RC"] == net, "box 1 RC output base = acquisition net"
    assert amounts["5_RC"] == vat, "box 5 RC deductible input VAT"
    assert amounts["6"] == net, "box 6 informative acquisition base"
