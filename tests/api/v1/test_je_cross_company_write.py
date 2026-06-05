"""Task 8 (service half) — service-layer cross-company write rejection.

Complements the DB-level structural probe (test_company_structural.py): a
journal_entries.create whose line references a sibling company's account is
rejected at the service layer (by _validate_lines_company + assert_company_owned)
with an opaque JournalEntryError, before any DB write.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services import journal_entries as svc

pytestmark = pytest.mark.postgres_only
_T = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def two_cos():
    a, b = uuid.uuid4(), uuid.uuid4()
    aa, ab, bb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as s:
        for cid in (a, b):
            s.add(Company(id=cid, tenant_id=_T, name=f"X {cid.hex[:6]}",
                          base_currency="AUD", fin_year_start_month=7,
                          audit_mode="immutable"))
        await s.flush()
        s.add(Account(id=aa, company_id=a, tenant_id=_T, code="1-1",
                      name="A asset", account_type=AccountType.ASSET))
        s.add(Account(id=ab, company_id=a, tenant_id=_T, code="6-1",
                      name="A exp", account_type=AccountType.EXPENSE))
        s.add(Account(id=bb, company_id=b, tenant_id=_T, code="1-1",
                      name="B asset", account_type=AccountType.ASSET))
        await s.commit()
    yield {"a": a, "b": b, "aa": aa, "ab": ab, "bb": bb}
    from sqlalchemy import delete as _del

    from saebooks.models.journal import JournalEntry as _JE
    async with AsyncSessionLocal() as s:
        await s.execute(_del(_JE).where(_JE.company_id.in_([a, b])))
        for cid in (a, b):
            co = await s.get(Company, cid)
            if co:
                await s.delete(co)
        await s.commit()


async def test_create_with_foreign_company_account_rejected(two_cos):
    d = two_cos
    async with AsyncSessionLocal() as s:
        with pytest.raises(svc.JournalEntryError):
            await svc.create(
                s, d["a"], _T, actor="t", entry_date=date(2026, 5, 1),
                narration="x",
                lines=[
                    {"account_id": str(d["aa"]), "debit": Decimal("5"), "credit": Decimal("0")},
                    {"account_id": str(d["bb"]), "debit": Decimal("0"), "credit": Decimal("5")},
                ],
            )


async def test_create_own_company_accounts_ok(two_cos):
    """The same shape with both accounts in company A succeeds (control)."""
    d = two_cos
    async with AsyncSessionLocal() as s:
        e = await svc.create(
            s, d["a"], _T, actor="t", entry_date=date(2026, 5, 1),
            narration="ok",
            lines=[
                {"account_id": str(d["aa"]), "debit": Decimal("5"), "credit": Decimal("0")},
                {"account_id": str(d["ab"]), "debit": Decimal("0"), "credit": Decimal("5")},
            ],
        )
        assert e.id is not None
        # the structural trigger pinned each line's company_id to A
        for ln in e.lines:
            assert ln.company_id == d["a"]
