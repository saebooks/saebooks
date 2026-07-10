"""Tests for journal entry service — create, post, reverse, balance, period-lock."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
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
            select(Account)
            .where(
                Account.company_id == company.id,
                Account.is_header.is_(False),
            )
            .order_by(Account.code)
            .limit(2)
        )
        a, b = accts.scalars().all()
        return company.id, a.id, b.id


async def test_create_draft_auto_ref() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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
        # Tenant must come from the caller — never silently fall back
        # to the legacy ``00000000`` default that the model column would
        # otherwise supply if the constructor was called bare.
        assert entry.tenant_id == DEFAULT_TENANT_ID


async def test_create_draft_rejects_missing_tenant_id() -> None:
    """Regression: passing ``tenant_id=None`` (or omitting it via **kwargs)
    must raise rather than silently fall through to the model server-default
    tenant ``00000000-0000-0000-0000-000000000001`` and leak the JE out of
    the caller's tenant scope. See the EX1237 / EX1263 BAS-refund leak
    that prompted this guard.
    """
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="create_draft requires tenant_id"):
            await svc.create_draft(
                session,
                company_id=company_id,
                tenant_id=None,  # type: ignore[arg-type]
                entry_date=date(2026, 4, 1),
                lines=[
                    {"account_id": acct_a, "debit": 1, "credit": 0},
                    {"account_id": acct_b, "debit": 0, "credit": 1},
                ],
            )


async def test_reverse_inherits_tenant_id_from_original() -> None:
    """Regression: the reversal JE must land under the original's tenant_id,
    not the request tenant (in case admin in a different tenant drives the
    reverse) and definitely not the model default. Pre-fix ``reverse``
    omitted ``tenant_id`` from the constructor and any reversal silently
    landed under tenant ``00000000``.
    """
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 4),
            lines=[
                {"account_id": acct_a, "debit": 250, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 250},
            ],
        )
        await svc.post(session, entry.id)

        reversal = await svc.reverse(session, entry.id, posted_by="test")
        assert reversal.tenant_id == DEFAULT_TENANT_ID
        # Pair must be atomic w.r.t. tenant — the original is REVERSED,
        # they must share tenant_id so any tenant-scoped reader sees both
        # or neither.
        original = await svc.get(session, entry.id)
        assert original.tenant_id == reversal.tenant_id


async def test_post_balanced_entry() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
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
            session, company_id=company_id, tenant_id=DEFAULT_TENANT_ID, entry_date=date(2026, 4, 2)
        )
        with pytest.raises(PostingError, match="no lines"):
            await svc.post(session, entry.id)


async def test_reverse_creates_mirror_entry() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 3, 20),
            lines=[
                {"account_id": acct_a, "debit": 75, "credit": 0},
                {"account_id": acct_b, "debit": 0, "credit": 75},
            ],
        )
        # F-04: an authorised role (admin/accountant/owner) plus a real
        # explanation now both required to bypass a period lock.
        posted = await svc.post(
            session,
            entry.id,
            override_reason="BAS amendment approved",
            actor_role="admin",
        )
        assert posted.status == EntryStatus.POSTED
        assert posted.override_reason == "BAS amendment approved"


# ---------------------------------------------------------------------------
# F-04 — period-lock override role gate
#
# Override into a closed period requires BOTH an authorised role
# (admin/accountant/owner) AND a non-trivial 12+ char reason. These
# tests cover every gate cell of the matrix below:
#
#   role × reason  | trivial | meaningful
#   ---------------+---------+-----------
#   admin          |  fail   |  pass
#   accountant     |  fail   |  pass
#   owner          |   —     |  pass
#   bookkeeper     |   —     |  fail (role)
#   viewer         |   —     |  fail (role)
#   None           |   —     |  fail (role, fail-closed)
#
# We also assert that the role gate fires BEFORE the reason gate when
# both are bad, so the operator gets the more actionable error
# ("I'm not allowed") rather than the secondary one ("reason too short").
# ---------------------------------------------------------------------------


async def _ctx_with_lock(
    lock_date: date,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, debit, credit) with a period lock at ``lock_date``.

    Uses a fresh isolated company so the lock doesn't pollute the rest of
    the test module's shared company fixture. Reuses CoA accounts from
    the seed.
    """
    async with AsyncSessionLocal() as session:
        # Look up the seed tenant for the FK.
        tenant_row = (
            await session.execute(select(Tenant).order_by(Tenant.created_at).limit(1))
        ).scalars().first()
        assert tenant_row is not None

        company = Company(
            tenant_id=tenant_row.id,
            name=f"f04_test_{uuid.uuid4().hex[:8]}",
            legal_name=f"F04 Test Co {uuid.uuid4().hex[:6]}",
        )
        session.add(company)
        await session.flush()

        # Two minimal accounts — bank (asset) and revenue (income).
        bank = Account(
            tenant_id=tenant_row.id,
            company_id=company.id,
            code=f"1-{uuid.uuid4().hex[:4]}",
            name="F04 Bank",
            account_type=AccountType.ASSET,
            reconcile=False,
        )
        revenue = Account(
            tenant_id=tenant_row.id,
            company_id=company.id,
            code=f"4-{uuid.uuid4().hex[:4]}",
            name="F04 Revenue",
            account_type=AccountType.INCOME,
        )
        session.add_all([bank, revenue])
        await session.flush()

        # Lock the period.
        await svc.lock_period(session, company.id, lock_date, locked_by="f04_test")
        await session.commit()

        return company.id, bank.id, revenue.id


async def _draft_in_locked_period(
    company_id: uuid.UUID,
    debit_id: uuid.UUID,
    credit_id: uuid.UUID,
    *,
    entry_date: date,
) -> uuid.UUID:
    """Create a balanced draft journal dated inside the locked period."""
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            lines=[
                {"account_id": debit_id, "debit": Decimal("100"), "credit": Decimal("0")},
                {"account_id": credit_id, "debit": Decimal("0"), "credit": Decimal("100")},
            ],
        )
        return entry.id


_GOOD_REASON = "Year-end accrual booked late by approval of CFO"


@pytest.mark.parametrize("role", ["admin", "accountant", "owner"])
async def test_f04_authorised_role_with_good_reason_succeeds(role: str) -> None:
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        posted = await svc.post(
            session,
            entry_id,
            override_reason=_GOOD_REASON,
            actor_role=role,
        )
        assert posted.status == EntryStatus.POSTED
        assert posted.override_reason == _GOOD_REASON


@pytest.mark.parametrize("role", ["bookkeeper", "viewer", "junk-role"])
async def test_f04_unauthorised_role_with_good_reason_rejected(role: str) -> None:
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="admin or accountant"):
            await svc.post(
                session,
                entry_id,
                override_reason=_GOOD_REASON,
                actor_role=role,
            )


async def test_f04_no_role_passed_fails_closed() -> None:
    """Default actor_role=None — period-lock override must be rejected."""
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="admin or accountant"):
            await svc.post(
                session,
                entry_id,
                override_reason=_GOOD_REASON,
                # actor_role intentionally omitted — must fail closed.
            )


async def test_f04_admin_with_short_reason_rejected() -> None:
    """12-char gate still fires for authorised roles (interim guard kept)."""
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="meaningful explanation"):
            await svc.post(
                session,
                entry_id,
                override_reason="too short",  # 9 chars
                actor_role="admin",
            )


async def test_f04_admin_with_stop_word_reason_rejected() -> None:
    """Stop-word gate still fires for authorised roles."""
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="meaningful explanation"):
            await svc.post(
                session,
                entry_id,
                # 12+ chars but the cleaned value normalises to the
                # stop-word "override" — must still be rejected.
                override_reason="  Override  ",
                actor_role="admin",
            )


async def test_f04_unauthorised_role_short_reason_role_error_first() -> None:
    """When BOTH gates would fire, role-gate error wins for usability."""
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    entry_id = await _draft_in_locked_period(
        company_id, debit_id, credit_id, entry_date=date(2020, 6, 15)
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="admin or accountant"):
            await svc.post(
                session,
                entry_id,
                override_reason="ok",  # would fail the 12-char gate too
                actor_role="bookkeeper",
            )


async def test_f04_post_into_open_period_role_irrelevant() -> None:
    """When the date is outside the lock, no role check fires at all.

    Confirms the gate only triggers on the lock path — a bookkeeper can
    still post a journal dated in an open period without supplying any
    override metadata.
    """
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2021, 1, 5),  # AFTER the lock — open period.
            lines=[
                {"account_id": debit_id, "debit": Decimal("50"), "credit": Decimal("0")},
                {"account_id": credit_id, "debit": Decimal("0"), "credit": Decimal("50")},
            ],
        )
        posted = await svc.post(session, entry.id)  # no role, no reason
        assert posted.status == EntryStatus.POSTED


async def test_f04_reverse_threads_actor_role() -> None:
    """``reverse()`` must pass actor_role into the inner post() so the
    reversal's auto-post inherits the gate.

    Setup: post an entry into an OPEN period (no override needed), then
    lock the period that contains the reversal date, then attempt to
    reverse with a non-privileged role. The reversal auto-post should
    fail with the role-gate error.
    """
    company_id, debit_id, credit_id = await _ctx_with_lock(date(2020, 6, 30))
    # Post a normal entry AFTER the lock so the original post needs no
    # override.
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2021, 1, 5),
            lines=[
                {"account_id": debit_id, "debit": Decimal("30"), "credit": Decimal("0")},
                {"account_id": credit_id, "debit": Decimal("0"), "credit": Decimal("30")},
            ],
        )
        await svc.post(session, entry.id)
        entry_id = entry.id

    # Now extend the lock past the original entry_date so the reversal
    # (which inherits the date) lands in the locked period.
    async with AsyncSessionLocal() as session:
        await svc.lock_period(
            session, company_id, date(2021, 12, 31), locked_by="f04_test_extend"
        )
        await session.commit()

    # Bookkeeper trying to reverse with an explanation — still blocked
    # because the role isn't on the override list.
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="admin or accountant"):
            await svc.reverse(
                session,
                entry_id,
                override_reason=_GOOD_REASON,
                actor_role="bookkeeper",
            )


async def test_cannot_reverse_draft() -> None:
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
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
    """Posting a JE that moves funds between trust and operating bank accounts
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
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
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
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 28),
            lines=[
                {"account_id": ex_id, "debit": 500, "credit": 0},
                {"account_id": tr_id, "debit": 0, "credit": 500},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED


async def test_trust_debit_to_revenue_blocked() -> None:
    """Dr Trust Bank / Cr Revenue must be blocked.

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
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 28),
            description="RLES2-RentToRevenue",
            lines=[
                {"account_id": tb_id, "debit": 2400, "credit": 0},
                {"account_id": rev_id, "debit": 0, "credit": 2400},
            ],
        )
        with pytest.raises(PostingError, match="not agency income"):
            await svc.post(session, entry.id, posted_by="test")


async def test_trust_debit_to_liability_allowed() -> None:
    """Positive control: Dr Trust Bank / Cr Trust Liability must post.

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
            tenant_id=DEFAULT_TENANT_ID,
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
    """create_draft with ref >32 chars must raise PostingError (not propagate as 500)."""
    company_id, acct_a, acct_b = await _ctx()
    long_ref = "A" * 33
    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="32 characters or less"):
            await svc.create_draft(
                session,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                entry_date=date(2026, 4, 28),
                ref=long_ref,
                lines=[
                    {"account_id": acct_a, "debit": 100, "credit": 0},
                    {"account_id": acct_b, "debit": 0, "credit": 100},
                ],
            )


async def test_ref_too_long_on_update_draft_raises_422() -> None:
    """update_draft with ref >32 chars must raise PostingError."""
    company_id, acct_a, acct_b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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
    """create_draft must reject line accounts that belong to a foreign
    company/tenant.

    Migration 0131_tenant_id_coherence_trigger now enforces at the DB
    layer that ``accounts.tenant_id == companies.tenant_id`` — so we
    can't (any more) construct the original "same company, different
    tenant" probe; the foreign Account insert is itself rejected. The
    equivalent attack surface is "borrow a foreign company's account
    into the home company's JE", which ``_validate_line_accounts``
    catches via the ``row.company_id != company_id`` check. That's
    what this test now asserts.
    """
    home_company_id, acct_a, _acct_b = await _ctx()
    home_tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    foreign_tenant_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.add(Tenant(
            id=foreign_tenant_id,
            name="Foreign Corp",
            slug=f"foreign-{foreign_tenant_id}",
        ))
        await session.flush()
        # Foreign company in the foreign tenant — needed to satisfy
        # migration 0131's tenant_id_coherence_trigger on accounts.
        foreign_company = Company(
            tenant_id=foreign_tenant_id,
            name=f"Foreign Co {str(foreign_tenant_id)[:6]}",
            base_currency="AUD",
            fin_year_start_month=7,
        )
        session.add(foreign_company)
        await session.flush()
        foreign_acct = Account(
            company_id=foreign_company.id,
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
                company_id=home_company_id,
                tenant_id=home_tenant_id,
                entry_date=date(2026, 4, 10),
                lines=[
                    {"account_id": acct_a, "debit": 100, "credit": 0},
                    {"account_id": foreign_acct_id, "debit": 0, "credit": 100},
                ],
            )


# ---------------------------------------------------------------------------
# PSI-3: related-party wages distribution guard (ITAA97 s.86-70)
# ---------------------------------------------------------------------------


async def _psi_ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, wages_acct_id, bank_acct_id, other_acct_id) for PSI tests.

    Self-contained: creates its own company + accounts so PSI tests don't
    depend on the shared _ctx() company having accounts seeded.
    """
    uid = str(uuid.uuid4())[:6].upper()
    async with AsyncSessionLocal() as session:
        tenant_result = await session.execute(
            select(Tenant).where(Tenant.id == _DEFAULT_TENANT)
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None:
            session.add(Tenant(
                id=_DEFAULT_TENANT,
                name="Default Tenant",
                slug="default",
            ))
            await session.flush()

        co = Company(name=f"PSI Test Co {uid}", legal_name=f"PSI Test Co {uid} Pty Ltd")
        session.add(co)
        await session.flush()
        company_id = co.id

        wages = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"6-243{uid[:3]}",
            name="Wages & Salaries — PSI test",
            account_type=AccountType.EXPENSE,
        )
        bank = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"1-PSI{uid[:3]}",
            name="Bank — PSI test",
            account_type=AccountType.ASSET,
            reconcile=True,
        )
        other = Account(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=f"6-OTH{uid[:3]}",
            name="Other Expense — PSI test",
            account_type=AccountType.EXPENSE,
        )
        session.add(wages)
        session.add(bank)
        session.add(other)
        await session.commit()
        return company_id, wages.id, bank.id, other.id


async def test_psi_spouse_wages_blocked_on_post() -> None:
    """Negative control: Dr Wages[6-243x] with 'spouse' in description must block."""
    company_id, wages_id, bank_id, _other_id = await _psi_ctx()

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 29),
            description="Wages payment to spouse $2,000",
            lines=[
                {"account_id": wages_id, "debit": 2000, "credit": 0},
                {"account_id": bank_id, "debit": 0, "credit": 2000},
            ],
        )
        with pytest.raises(PostingError, match="PSI"):
            await svc.post(session, entry.id, posted_by="test")


async def test_psi_related_party_line_description_blocked() -> None:
    """Related-party indicator in line description (not entry) also triggers."""
    company_id, wages_id, bank_id, _other_id = await _psi_ctx()

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 29),
            lines=[
                {"account_id": wages_id, "debit": 1500, "credit": 0,
                 "description": "Related party — family member wage"},
                {"account_id": bank_id, "debit": 0, "credit": 1500},
            ],
        )
        with pytest.raises(PostingError, match="PSI"):
            await svc.post(session, entry.id, posted_by="test")


async def test_psi_override_reason_allows_post() -> None:
    """Providing override_reason records compliance and allows post."""
    company_id, wages_id, bank_id, _other_id = await _psi_ctx()

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 29),
            description="Wages — spouse, PAYG-W withheld as per STP",
            lines=[
                {"account_id": wages_id, "debit": 2000, "credit": 0},
                {"account_id": bank_id, "debit": 0, "credit": 2000},
            ],
        )
        posted = await svc.post(
            session, entry.id, posted_by="test",
            override_reason="PAYG-W withholding applied; business determination in place"
        )
        assert posted.status == EntryStatus.POSTED
        assert posted.override_reason is not None


async def test_psi_unrelated_contractor_wages_allowed() -> None:
    """Positive control: wages to unrelated contractor post without warning."""
    company_id, wages_id, bank_id, _other_id = await _psi_ctx()

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 29),
            description="Consulting fee — Dr Wages / Cr Bank $500 to Consultant Name",
            lines=[
                {"account_id": wages_id, "debit": 500, "credit": 0},
                {"account_id": bank_id, "debit": 0, "credit": 500},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED


async def test_psi_non_wages_account_not_flagged() -> None:
    """'spouse' in description for a non-wages account does not trigger."""
    company_id, _wages_id, bank_id, other_id = await _psi_ctx()

    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 29),
            description="Transfer to spouse bank account — personal",
            lines=[
                {"account_id": other_id, "debit": 100, "credit": 0},
                {"account_id": bank_id, "debit": 0, "credit": 100},
            ],
        )
        # Should not raise — other_id is not a 6-243x wages account
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED
