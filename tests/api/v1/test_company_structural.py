"""Task 5 — DB-level structural guards from migration 0152.

Red before 0152 (the inserts succeed / the column is absent); green after:
* a journal_line in company A referencing company B's account is rejected by
  the composite (account_id, company_id) -> accounts(id, company_id) FK;
* a journal_entries row whose tenant_id != its company's tenant is rejected
  by the tenant<->company coherence trigger (0131-style, added for JEs here);
* a line that omits company_id is auto-pinned to its parent entry's company.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only
_T = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def two_companies():
    a, b = uuid.uuid4(), uuid.uuid4()
    aa, bb = uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as s:
        for cid in (a, b):
            s.add(Company(id=cid, tenant_id=_T, name=f"S {cid.hex[:6]}",
                          base_currency="AUD", fin_year_start_month=7,
                          audit_mode="immutable"))
        await s.flush()
        s.add(Account(id=aa, company_id=a, tenant_id=_T, code="1-1", name="A",
                      account_type=AccountType.ASSET))
        s.add(Account(id=bb, company_id=b, tenant_id=_T, code="1-1", name="B",
                      account_type=AccountType.ASSET))
        await s.commit()
    yield {"a": a, "b": b, "acct_a": aa, "acct_b": bb}
    from sqlalchemy import delete as _del

    from saebooks.models.journal import JournalEntry as _JE
    async with AsyncSessionLocal() as s:
        await s.execute(_del(_JE).where(_JE.company_id.in_([a, b])))
        for cid in (a, b):
            co = await s.get(Company, cid)
            if co:
                await s.delete(co)
        await s.commit()


async def test_composite_fk_blocks_foreign_account_line(two_companies):
    d = two_companies
    eid = uuid.uuid4()
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "INSERT INTO journal_entries (id, company_id, tenant_id, ref, "
            "entry_date, status, version, created_at, updated_at) "
            "VALUES (:id, :c, :t, 'JE-X', '2026-05-01', 'DRAFT', 1, now(), now())"
        ).bindparams(id=eid, c=d["a"], t=_T))
        with pytest.raises(DBAPIError):
            # company A line referencing company B's account -> composite FK reject
            await s.execute(text(
                "INSERT INTO journal_lines (id, entry_id, company_id, line_no, "
                "account_id, debit, credit) "
                "VALUES (:id, :e, :c, 1, :acct, 5, 0)"
            ).bindparams(id=uuid.uuid4(), e=eid, c=d["a"], acct=d["acct_b"]))
            await s.commit()


async def test_je_coherence_trigger_blocks_tenant_mismatch(two_companies):
    d = two_companies
    async with AsyncSessionLocal() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text(
                "INSERT INTO journal_entries (id, company_id, tenant_id, ref, "
                "entry_date, status, version, created_at, updated_at) "
                "VALUES (:id, :c, :t, 'JE-Y', '2026-05-01', 'DRAFT', 1, now(), now())"
            ).bindparams(id=uuid.uuid4(), c=d["a"], t=uuid.uuid4()))
            await s.commit()


async def test_line_company_autofilled_from_parent(two_companies):
    """A line that omits company_id is pinned to its parent entry's company."""
    d = two_companies
    eid, lid = uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "INSERT INTO journal_entries (id, company_id, tenant_id, ref, "
            "entry_date, status, version, created_at, updated_at) "
            "VALUES (:id, :c, :t, 'JE-Z', '2026-05-01', 'DRAFT', 1, now(), now())"
        ).bindparams(id=eid, c=d["a"], t=_T))
        await s.execute(text(
            "INSERT INTO journal_lines (id, entry_id, line_no, account_id, "
            "debit, credit) VALUES (:id, :e, 1, :acct, 5, 0)"
        ).bindparams(id=lid, e=eid, acct=d["acct_a"]))
        await s.commit()
        got = (await s.execute(text(
            "SELECT company_id FROM journal_lines WHERE id = :id"
        ).bindparams(id=lid))).scalar_one()
    assert got == d["a"]
