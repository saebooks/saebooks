"""Tests for journal entry service — create, post, reverse, balance, period-lock."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.models.tenant import Tenant
from saebooks.services import journal as svc
from saebooks.services.journal import PostingError


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, debit_account_id, credit_account_id)."""
    async with AsyncSessionLocal() as session:
        co = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = co.scalars().first()
        assert company is not None

        accts = await session.execute(
            select(Account).where(Account.company_id == company.id).order_by(Account.code).limit(2)
        )
        a, b = accts.scalars().all()
        return company.id, a.id, b.id


async def test_create_draft_auto_ref() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 1),
            description="Test entry",
            lines=[
                {"account_id": acct_a, "debit": 100, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 100},
            ],
        )
        assert entry.ref.startswith("JE-")
        assert entry.status == EntryStatus.DRAFT
        assert len(entry.lines) == 2


async def test_post_balanced_entry() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 1),
            lines=[
                {"account_id": acct_a, "debit": Decimal("250.75"), "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": Decimal("250.75")},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED
        assert posted.posted_at is not None


async def test_post_unbalanced_rejected() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 2),
            lines=[
                {"account_id": acct_a, "debit": 100, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 50},
            ],
        )
        with pytest.raises(PostingError, match="unbalanced"):
            await svc.post(session, entry.id)


async def test_post_empty_entry_rejected() -> None:
    company_id, _acct_a, _acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session, company_id=company_id, entry_date=date(2026, 4, 2)
        )
        with pytest.raises(PostingError, match="no lines"):
            await svc.post(session, entry.id)


async def test_reverse_creates_mirror_entry() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 3),
            lines=[
                {"account_id": acct_a, "debit": 500, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 500},
            ],
        )
        await svc.post(session, entry.id)

        reversal = await svc.reverse(session, entry.id, posted_by="test")
        assert reversal.status == EntryStatus.POSTED
        assert reversal.reversal_of_id == entry.id
        assert "Reversal" in (reversal.description or "")

        # Lines are mirrored
        assert reversal.lines[0].credit == Decimal("500")
        assert reversal.lines[1].debit == Decimal("500")

        # Original marked reversed
        original = await svc.get(session, entry.id)
        assert original.status == EntryStatus.REVERSED


async def test_period_lock_blocks_post() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        await svc.lock_period(
            session, company_id, date(2026, 3, 31), locked_by="test"
        )

        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 3, 15),
            lines=[
                {"account_id": acct_a, "debit": 100, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 100},
            ],
        )
        with pytest.raises(PostingError, match="locked"):
            await svc.post(session, entry.id)


async def test_period_lock_override_with_reason() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        # Lock already set from test above — date 2026-03-31
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 3, 20),
            lines=[
                {"account_id": acct_a, "debit": 75, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 75},
            ],
        )
        posted = await svc.post(
            session, entry.id, override_reason="BAS amendment approved"
        )
        assert posted.status == EntryStatus.POSTED
        assert posted.override_reason == "BAS amendment approved"


async def test_cannot_reverse_draft() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 4),
            lines=[
                {"account_id": acct_a, "debit": 10, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 10},
            ],
        )
        with pytest.raises(PostingError, match="posted"):
            await svc.reverse(session, entry.id)


async def test_gst_amount_on_lines() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 5),
            lines=[
                {
                    "account_id": acct_a,
                    "debit": Decimal("110"),
                    "credit": 0,
                    "gst_amount": Decimal("10.00"),
                },
                {"account_id": acct_b, "debit": 0, "credit": Decimal("110")},
            ],
        )
        assert entry.lines[0].gst_amount == Decimal("10.00")
        assert entry.lines[1].gst_amount is None


_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_trust_commingling_blocked_on_post() -> None:
    """gap RLES-1: posting a JE that moves funds between trust and operating bank accounts
    must raise PostingError — commingling is a NSW Property Act breach."""
    company_id, _a, _b = await _ctx()
    uid = str(uuid.uuid4())[:6].upper()

    async with AsyncSessionLocal() as session:
        trust_acct = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-T{uid}",
            name="Trust — Sales Deposits",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        op_acct = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-O{uid}",
            name="Bank — Operating",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=False,
        )
        session.add(trust_acct)
        session.add(op_acct)
        await session.commit()
        trust_id = trust_acct.id
        op_id = op_acct.id

    # Dr Operating / Cr Trust — the RLES-1 commingling pattern — must be blocked.
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            description="RLES28-Comingle",
            lines=[
                {"account_id": op_id, "debit": 52000, "credit": 0},
                {"account_id": trust_id, "debit": 0, "credit": 52000},
            ],
        )
        with pytest.raises(PostingError, match="trust"):
            await svc.post(session, entry.id, posted_by="test")


async def test_trust_to_trust_transfer_allowed() -> None:
    """A JE moving funds between two trust bank accounts is not commingling — must post."""
    company_id, _a, _b = await _ctx()
    uid = str(uuid.uuid4())[:6].upper()

    async with AsyncSessionLocal() as session:
        trust1 = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-A{uid}",
            name="Trust — Sales A",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        trust2 = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-B{uid}",
            name="Trust — Sales B",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        session.add(trust1)
        session.add(trust2)
        await session.commit()
        t1_id = trust1.id
        t2_id = trust2.id

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            lines=[
                {"account_id": t1_id, "debit": 1000, "credit": 0},
                {"account_id": t2_id, "debit": 0, "credit": 1000},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED


async def test_trust_payment_to_expense_allowed() -> None:
    """Dr Expense / Cr Trust is a valid trust disbursement — must not be blocked."""
    company_id, _a, _b = await _ctx()
    uid = str(uuid.uuid4())[:6].upper()

    async with AsyncSessionLocal() as session:
        trust_acct = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-E{uid}",
            name="Trust — Expense Test",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        expense_acct = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"6-E{uid}",
            name="Trust Expenses Payable",
            account_type=AccountType.EXPENSE,
            reconcile=False,
        )
        session.add(trust_acct)
        session.add(expense_acct)
        await session.commit()
        tr_id = trust_acct.id
        ex_id = expense_acct.id

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            lines=[
                {"account_id": ex_id, "debit": 500, "credit": 0},
                {"account_id": tr_id, "debit": 0, "credit": 500},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED


async def test_trust_debit_to_revenue_blocked() -> None:
    """gap RLES-2: Dr Trust Bank / Cr Revenue must be blocked.

    Rent collected on behalf of landlords is trust money — crediting a
    revenue account inflates BAS G1 and misrepresents agency turnover.
    """
    company_id, _a, _b = await _ctx()
    uid = str(uuid.uuid4())[:6].upper()

    async with AsyncSessionLocal() as session:
        trust_bank = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-TRB{uid}",
            name="Trust Bank — Rent",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        revenue_acct = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"4-RNT{uid}",
            name="Rent Revenue",
            account_type=AccountType.INCOME,
            reconcile=False,
        )
        session.add(trust_bank)
        session.add(revenue_acct)
        await session.commit()
        tb_id = trust_bank.id
        rev_id = revenue_acct.id

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            description="RLES2-RentToRevenue",
            lines=[
                {"account_id": tb_id, "debit": 2400, "credit": 0},
                {"account_id": rev_id, "debit": 0, "credit": 2400},
            ],
        )
        with pytest.raises(PostingError, match="RLES-2"):
            await svc.post(session, entry.id, posted_by="test")


async def test_trust_debit_to_liability_allowed() -> None:
    """gap RLES-2 positive control: Dr Trust Bank / Cr Trust Liability must post.

    This is the correct pattern for receiving rent on behalf of a landlord.
    """
    company_id, _a, _b = await _ctx()
    uid = str(uuid.uuid4())[:6].upper()

    async with AsyncSessionLocal() as session:
        trust_bank = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-TBL{uid}",
            name="Trust Bank — Rent",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        trust_liability = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"2-OTL{uid}",
            name="Landlord / Owner Trust Liability",
            account_type=AccountType.LIABILITY,
            reconcile=False,
        )
        session.add(trust_bank)
        session.add(trust_liability)
        await session.commit()
        tb_id = trust_bank.id
        tl_id = trust_liability.id

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            description="RLES2-RentToTrustLiability",
            lines=[
                {"account_id": tb_id, "debit": 2400, "credit": 0},
                {"account_id": tl_id, "debit": 0, "credit": 2400},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED


async def test_ref_too_long_on_create_draft_raises_422() -> None:
    """gap RLES-7: create_draft with ref >32 chars must raise PostingError (not propagate as 500)."""
    company_id, acct_a, acct_b = await _ctx()
    long_ref = "A" * 33
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="32 characters or less"):
            await svc.create_draft(
                session,
                company_id=company_id,
                entry_date=date(2026, 4, 28),
                ref=long_ref,
                lines=[
                    {"account_id": acct_a, "debit": 100, "credit": 0},
                    {"account_id": acct_b, "debit": 0, "credit": 100},
                ],
            )


async def test_ref_too_long_on_update_draft_raises_422() -> None:
    """gap RLES-7: update_draft with ref >32 chars must raise PostingError."""
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 4, 28),
            lines=[
                {"account_id": acct_a, "debit": 50, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 50},
            ],
        )
        entry_id = entry.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="32 characters or less"):
            await svc.update_draft(
                session,
                entry_id,
                ref="B" * 33,
            )


async def test_cross_tenant_account_rejected_on_create_draft() -> None:
    """create_draft must reject line accounts from a foreign tenant (gap PRTR-1)."""
    company_id, acct_a, _acct_b = await _ctx()
    home_tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    foreign_tenant_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.add(Tenant(
            id=foreign_tenant_id,
            name="Foreign Corp",
            slug=f"foreign-{foreign_tenant_id}",
        ))
        await session.flush()
        foreign_acct = Account(
            company_id=company_id,
            tenant_id=foreign_tenant_id,
            code=f"9-XT{str(foreign_tenant_id)[:4].upper()}",
            name="Cross-Tenant Test Account",
            account_type=AccountType.EXPENSE,
        )
        session.add(foreign_acct)
        await session.commit()
        foreign_acct_id = foreign_acct.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="do not belong"):
            await svc.create_draft(
                session,
                company_id=company_id,
                entry_date=date(2026, 4, 10),
                tenant_id=home_tenant_id,
                lines=[
                    {"account_id": acct_a, "debit": 100, "credit": 0},
                    {"account_id": foreign_acct_id, "debit": 0, "credit": 100},
                ],
            )
