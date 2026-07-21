"""Regression: a supplier credit note settles its linked bill on BOTH backends.

The existing ``test_supplier_credit_note_settles_bill.py`` is ``postgres_only``
(it leans on the AU-seeded ledger). This one seeds a minimal ledger by hand so
it runs on the SQLite/Community backend too — the edition where posting was
actually blocked: the SCN post writes a change_log row, and ``change_log.id``
(a ``BIGINT PRIMARY KEY`` that does not autoincrement on SQLite) made every post
500 *before* the settle ran. With that fixed, the (already-correct)
``_refresh_bill_amount_paid`` relieves the bill.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import settings as settings_svc
from saebooks.services import supplier_credit_notes as scn_svc

_T = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as s:
        c = Company(id=uuid.uuid4(), tenant_id=_T, name="SCN Both-Backend Co")
        s.add(c)
        await s.flush()
        exp = Account(
            company_id=c.id, tenant_id=_T, code="6-1000", name="Expense",
            account_type=AccountType.EXPENSE,
        )
        ap = Account(
            company_id=c.id, tenant_id=_T, code="2-1200", name="Trade Creditors",
            account_type=AccountType.LIABILITY,
        )
        gstp = Account(
            company_id=c.id, tenant_id=_T, code="1-1180", name="GST Paid",
            account_type=AccountType.ASSET,
        )
        tc = TaxCode(
            company_id=c.id, tenant_id=_T, code="GST", name="GST", rate=Decimal("10.000")
        )
        ct = Contact(
            company_id=c.id, tenant_id=_T, name="Sup", contact_type=ContactType.SUPPLIER
        )
        s.add_all([exp, ap, gstp, tc, ct])
        await s.commit()
        ids = (c.id, c.tenant_id, ct.id, exp.id, tc.id)
    async with AsyncSessionLocal() as s:
        await settings_svc.set(s, "gst_paid_account_code", "1-1180")
    return ids


async def test_supplier_credit_note_settles_bill_both_backends() -> None:
    cid, tid, ctid, expid, gstid = await _seed()
    line = {
        "description": "L", "account_id": expid, "tax_code_id": gstid,
        "quantity": Decimal("1"), "unit_price": Decimal("100"),
        "discount_pct": Decimal("0"),
    }

    async with AsyncSessionLocal() as s:
        bill = await bill_svc.create_draft(
            s, company_id=cid, contact_id=ctid,
            issue_date=date(2026, 4, 20), due_date=date(2026, 5, 20), lines=[line],
        )
        bid = bill.id
    async with AsyncSessionLocal() as s:
        await bill_svc.post_bill(s, bid, posted_by="test")
    async with AsyncSessionLocal() as s:
        bill = await s.get(Bill, bid)
        total = bill.total
        assert total == Decimal("110.00")
        assert bill.amount_paid == Decimal("0.00")

    async with AsyncSessionLocal() as s:
        scn = await scn_svc.api_create(
            s, company_id=cid, tenant_id=tid, actor="test", contact_id=ctid,
            issue_date=date(2026, 4, 21), lines=[line], original_bill_id=bid,
        )
        sid, sver = scn.id, scn.version
    async with AsyncSessionLocal() as s:
        await scn_svc.api_post(s, sid, "test", sver, tenant_id=tid, company_id=cid)

    async with AsyncSessionLocal() as s:
        bill = await s.get(Bill, bid)
        assert bill.amount_paid == total, "SCN did not settle the bill"
        assert bill.total - bill.amount_paid == Decimal("0.00")
