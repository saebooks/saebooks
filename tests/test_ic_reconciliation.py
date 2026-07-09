"""Phase 3d intercompany reconciliation — read-only position view.

Covers ``saebooks.services.ic_relay.recon.intercompany_position``:

* a posted LOCAL pair shows up matched (both ORIGINATOR + COUNTERPARTY legs),
  with its two legs and no relay state (LOCAL never rides the relay);
* a single-leg txn (a synthetic half-pair) is flagged unmatched;
* the call is READ-ONLY — it returns the same rows and mutates nothing
  (row counts in ic_txn/ic_legs unchanged before/after).

Uses the superuser AsyncSessionLocal like test_intercompany.py's service tests
(the recon service itself only SELECTs; the cross-tenant RLS guarantee is proven
in test_ic_remote_relay.py). Postgres only — depends on the 0154 composite FKs
and 0159 tables.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcLeg,
    IcLegSide,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import intercompany as ic_svc
from saebooks.services.ic_relay import recon as recon_svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture
async def local_pair_setup() -> AsyncIterator[dict[str, Any]]:
    """Two companies in the DEFAULT tenant + a reciprocal ic_edges pair.

    Mirrors test_intercompany.py's local_pair_setup so we can post a real LOCAL
    pair and reconcile it.
    """
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        orig = Company(name=f"Recon-P-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        cpty = Company(name=f"Recon-S-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        session.add_all([orig, cpty])
        await session.flush()

        orig_control = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                               code=f"1-15{tag[:2]}", name="Loan to SAE",
                               account_type=AccountType.ASSET)
        orig_contra = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        cpty_control = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                               code=f"2-22{tag[:2]}", name="Directors Loan",
                               account_type=AccountType.LIABILITY)
        cpty_contra = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        session.add_all([orig_control, orig_contra, cpty_control, cpty_contra])
        await session.flush()

        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=orig.id,
                           partner_company_id=cpty.id,
                           control_account_id=orig_control.id,
                           direction=IcEdgeDirection.ORIGINATOR))
        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=cpty.id,
                           partner_company_id=orig.id,
                           control_account_id=cpty_control.id,
                           direction=IcEdgeDirection.COUNTERPARTY))
        await session.commit()

        data = {
            "orig_id": orig.id, "cpty_id": cpty.id,
            "orig_contra": orig_contra.id, "cpty_contra": cpty_contra.id,
        }
    yield data

    async with AsyncSessionLocal() as session:
        for cid in (data["orig_id"], data["cpty_id"]):
            await session.execute(text("DELETE FROM ic_legs WHERE company_id = :c"),
                                  {"c": cid})
        await session.execute(
            text("DELETE FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(
            text("DELETE FROM ic_edges WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(
            text("DELETE FROM journal_lines WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(
            text("DELETE FROM journal_entries WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(
            text("DELETE FROM accounts WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(
            text("DELETE FROM companies WHERE id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.commit()


async def test_matched_local_pair(local_pair_setup: dict[str, Any]) -> None:
    d = local_pair_setup
    async with AsyncSessionLocal() as session:
        ic_txn = await ic_svc.post_local_pair(
            session,
            tenant_id=_DEFAULT_TENANT,
            originator_company_id=d["orig_id"],
            counterparty_company_id=d["cpty_id"],
            amount=Decimal("5000.00"),
            entry_date=date(2026, 6, 6),
            description="Recon matched pair",
            originator_contra_account_id=d["orig_contra"],
            counterparty_contra_account_id=d["cpty_contra"],
            posted_by="test",
        )

    async with AsyncSessionLocal() as session:
        rows = await recon_svc.intercompany_position(
            session, tenant_id=_DEFAULT_TENANT, company_id=d["orig_id"]
        )
    by_id = {r.ic_txn_id: r for r in rows}
    assert ic_txn.id in by_id, "posted pair missing from reconciliation"
    row = by_id[ic_txn.id]
    assert row.matched is True, "a complete LOCAL pair must be matched"
    assert len(row.legs) == 2
    assert {leg.side for leg in row.legs} == {
        str(IcLegSide.ORIGINATOR), str(IcLegSide.COUNTERPARTY)
    }
    # LOCAL pair never rides the relay — no outbox/inbox state.
    assert row.outbox_status is None and row.inbox_status is None


async def test_unmatched_single_leg_flagged(local_pair_setup: dict[str, Any]) -> None:
    """A txn with only one leg is flagged unmatched (the half-pair surface)."""
    d = local_pair_setup
    txn_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        # Minimal DRAFT JE (via the ORM so version/defaults are set) to satisfy
        # the ic_legs FK, then a SINGLE leg — a deliberate half-pair. The recon
        # view reads legs by side and does not require a POSTED/balanced JE.
        je = JournalEntry(
            id=uuid.uuid4(), company_id=d["orig_id"], tenant_id=_DEFAULT_TENANT,
            ref=f"JE-RECON-{uuid.uuid4().hex[:6]}", entry_date=date(2026, 6, 6),
            status=EntryStatus.DRAFT,
        )
        session.add(je)
        await session.flush()
        session.add(IcTxn(id=txn_id, tenant_id=_DEFAULT_TENANT,
                          company_id=d["orig_id"], description="half pair",
                          status=IcTxnStatus.ACTIVE))
        await session.flush()
        session.add(IcLeg(tenant_id=_DEFAULT_TENANT, company_id=d["orig_id"],
                          ic_txn_id=txn_id, journal_entry_id=je.id,
                          side=IcLegSide.ORIGINATOR))
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await recon_svc.intercompany_position(
            session, tenant_id=_DEFAULT_TENANT, company_id=d["orig_id"]
        )
    by_id = {r.ic_txn_id: r for r in rows}
    assert txn_id in by_id
    assert by_id[txn_id].matched is False, "single-leg txn must be unmatched"
    assert len(by_id[txn_id].legs) == 1


async def test_reconciliation_is_read_only(local_pair_setup: dict[str, Any]) -> None:
    """Calling the view must not change ic_txn / ic_legs row counts."""
    d = local_pair_setup
    async with AsyncSessionLocal() as session:
        await ic_svc.post_local_pair(
            session, tenant_id=_DEFAULT_TENANT,
            originator_company_id=d["orig_id"],
            counterparty_company_id=d["cpty_id"],
            amount=Decimal("1000.00"), entry_date=date(2026, 6, 6),
            description="read-only probe",
            originator_contra_account_id=d["orig_contra"],
            counterparty_contra_account_id=d["cpty_contra"],
            posted_by="test",
        )

    async def _counts() -> tuple[int, int]:
        async with AsyncSessionLocal() as session:
            t = (await session.execute(
                select(IcTxn).where(IcTxn.company_id == d["orig_id"])
            )).scalars().all()
            legs = (await session.execute(
                select(IcLeg).where(IcLeg.company_id == d["orig_id"])
            )).scalars().all()
            return len(t), len(legs)

    before = await _counts()
    async with AsyncSessionLocal() as session:
        await recon_svc.intercompany_position(
            session, tenant_id=_DEFAULT_TENANT, company_id=d["orig_id"]
        )
    after = await _counts()
    assert before == after, (
        f"reconciliation mutated state: txn/legs {before} -> {after}"
    )
