"""Tests for ``saebooks.services.reports.pl_by_segment``.

Covers:

* Project-segmented P&L rolls up per project when ``project_id`` is
  stamped on journal lines.
* Lines without a project_id land in the "Unassigned" bucket.
* Date window filters out lines outside the period.
* Unsupported segment (e.g. ``contact``) raises.

We post our own journal entries directly via the journal service so
the test is independent of the invoice / bill / payment plumbing —
the feature under test is the reporting aggregation, not the posters.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.project import Project
from saebooks.services import journal as journal_svc
from saebooks.services import projects as project_svc
from saebooks.services import reports as svc

TEST_PROJECT_PREFIX = "SEG-TEST"


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, income_account_id, expense_account_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        async def _first_of(t: AccountType) -> uuid.UUID:
            acct = (
                await session.execute(
                    select(Account)
                    .where(
                        Account.company_id == company.id,
                        Account.account_type == t,
                        Account.is_header.is_(False),
                    )
                    .order_by(Account.code)
                )
            ).scalars().first()
            assert acct is not None
            return acct.id

        return (
            company.id,
            await _first_of(AccountType.INCOME),
            await _first_of(AccountType.EXPENSE),
        )


async def _mk_project(company_id: uuid.UUID, suffix: str) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        p = await project_svc.create(
            session, company_id,
            code=f"{TEST_PROJECT_PREFIX}-{suffix}",
            name=f"Segment test {suffix}",
        )
    return p.id


async def _post_je(
    company_id: uuid.UUID,
    entry_date: date,
    *,
    income_id: uuid.UUID,
    expense_id: uuid.UUID,
    project_id: uuid.UUID | None,
    amount: Decimal,
) -> uuid.UUID:
    """Post a balanced JE: Dr Expense / Cr Income, both with (or without)
    the same project tag so both legs roll up to the same segment."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"segment-test {amount}",
            lines=[
                {
                    "account_id": expense_id,
                    "debit": amount,
                    "credit": Decimal("0"),
                    "project_id": project_id,
                },
                {
                    "account_id": income_id,
                    "debit": Decimal("0"),
                    "credit": amount,
                    "project_id": project_id,
                },
            ],
        )
        posted = await journal_svc.post(session, entry.id, posted_by="tests")
        return posted.id


# ---------------------------------------------------------------------- #
# Service                                                                 #
# ---------------------------------------------------------------------- #


async def test_pl_by_segment_rolls_up_per_project() -> None:
    cid, income, expense = await _ctx()
    proj_a = await _mk_project(cid, "A")
    proj_b = await _mk_project(cid, "B")

    # Post $100 against A, $200 against B, both on 2099-03-01.
    await _post_je(
        cid, date(2099, 3, 1),
        income_id=income, expense_id=expense,
        project_id=proj_a, amount=Decimal("100"),
    )
    await _post_je(
        cid, date(2099, 3, 1),
        income_id=income, expense_id=expense,
        project_id=proj_b, amount=Decimal("200"),
    )

    async with AsyncSessionLocal() as session:
        rows = await svc.pl_by_segment(
            session, cid,
            from_date=date(2099, 1, 1),
            to_date=date(2099, 12, 31),
            segment="project",
        )
    by_id = {r.segment_id: r for r in rows if r.segment_id is not None}
    assert proj_a in by_id
    assert proj_b in by_id
    # Each segment has both an INCOME and an EXPENSE section and both
    # total to the same amount — so net_profit is zero per segment.
    assert by_id[proj_a].net_profit == Decimal("0")
    assert by_id[proj_b].net_profit == Decimal("0")
    # Income section on project A totals 100 (credit-normal → negative balance)
    income_sections = [
        s for s in by_id[proj_a].sections
        if s.account_type == AccountType.INCOME
    ]
    assert len(income_sections) == 1
    assert abs(income_sections[0].total_balance) == Decimal("100")


async def test_pl_by_segment_unassigned_bucket_for_untagged() -> None:
    cid, income, expense = await _ctx()
    # Post with no project tag — lands in Unassigned
    await _post_je(
        cid, date(2099, 4, 1),
        income_id=income, expense_id=expense,
        project_id=None, amount=Decimal("55"),
    )
    async with AsyncSessionLocal() as session:
        rows = await svc.pl_by_segment(
            session, cid,
            from_date=date(2099, 4, 1),
            to_date=date(2099, 4, 30),
            segment="project",
        )
    # Unassigned row present
    unassigned = [r for r in rows if r.segment_id is None]
    assert len(unassigned) == 1
    assert unassigned[0].segment_label == "Unassigned"


async def test_pl_by_segment_date_window_filters_lines() -> None:
    cid, income, expense = await _ctx()
    proj = await _mk_project(cid, "DATE")

    await _post_je(
        cid, date(2099, 1, 15),
        income_id=income, expense_id=expense,
        project_id=proj, amount=Decimal("10"),
    )
    await _post_je(
        cid, date(2099, 7, 15),
        income_id=income, expense_id=expense,
        project_id=proj, amount=Decimal("20"),
    )

    async with AsyncSessionLocal() as session:
        # Only H1 window → only the $10 JE should roll up
        rows = await svc.pl_by_segment(
            session, cid,
            from_date=date(2099, 1, 1),
            to_date=date(2099, 6, 30),
            segment="project",
        )
    matched = next((r for r in rows if r.segment_id == proj), None)
    assert matched is not None
    income_sections = [
        s for s in matched.sections if s.account_type == AccountType.INCOME
    ]
    assert abs(income_sections[0].total_balance) == Decimal("10")


async def test_pl_by_segment_rejects_unsupported_segment() -> None:
    cid, _, _ = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="Unsupported segment"):
            await svc.pl_by_segment(
                session, cid, segment="contact",
            )


async def test_pl_by_segment_department_returns_rows() -> None:
    """Department-tagged lines appear under the correct segment label."""
    from saebooks.models.department import Department
    from saebooks.db import AsyncSessionLocal as S

    cid, income, expense = await _ctx()

    # Create a department directly in the DB.
    async with S() as session:
        # Need a tenant for the FK.
        from saebooks.models.tenant import Tenant
        tenant = (await session.execute(select(Tenant).limit(1))).scalars().first()
        assert tenant is not None
        dept = Department(
            id=uuid.uuid4(),
            company_id=cid,
            tenant_id=tenant.id,
            code="DEPT-SEG-A",
            name="Segment Dept A",
        )
        session.add(dept)
        await session.commit()
        dept_id = dept.id

    # Post a JE with department_id on both lines.
    async with S() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2099, 5, 1),
            description="dept-segment-test",
            lines=[
                {
                    "account_id": expense,
                    "debit": Decimal("300"),
                    "credit": Decimal("0"),
                    "department_id": dept_id,
                },
                {
                    "account_id": income,
                    "debit": Decimal("0"),
                    "credit": Decimal("300"),
                    "department_id": dept_id,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="tests")

    async with S() as session:
        rows = await svc.pl_by_segment(
            session, cid,
            from_date=date(2099, 5, 1),
            to_date=date(2099, 5, 31),
            segment="department",
        )

    dept_rows = [r for r in rows if r.segment_id == dept_id]
    assert dept_rows, "Department segment not found in report"
    assert "DEPT-SEG-A" in dept_rows[0].segment_label

    # Cleanup
    async with S() as session:
        entries = (await session.execute(
            select(JournalEntry).where(JournalEntry.description == "dept-segment-test")
        )).scalars().all()
        for e in entries:
            await session.execute(delete(JournalLine).where(JournalLine.entry_id == e.id))
            await session.delete(e)
        from saebooks.models.department import Department as D
        d = await session.get(D, dept_id)
        if d:
            await session.delete(d)
        await session.commit()


async def test_pl_by_segment_cost_centre_returns_rows() -> None:
    """Cost-centre-tagged lines appear under the correct segment label."""
    from saebooks.models.department import CostCentre
    from saebooks.db import AsyncSessionLocal as S

    cid, income, expense = await _ctx()

    async with S() as session:
        from saebooks.models.tenant import Tenant
        tenant = (await session.execute(select(Tenant).limit(1))).scalars().first()
        assert tenant is not None
        cc = CostCentre(
            id=uuid.uuid4(),
            company_id=cid,
            tenant_id=tenant.id,
            code="CC-SEG-B",
            name="Segment CC B",
        )
        session.add(cc)
        await session.commit()
        cc_id = cc.id

    async with S() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2099, 6, 1),
            description="cc-segment-test",
            lines=[
                {
                    "account_id": expense,
                    "debit": Decimal("150"),
                    "credit": Decimal("0"),
                    "cost_centre_id": cc_id,
                },
                {
                    "account_id": income,
                    "debit": Decimal("0"),
                    "credit": Decimal("150"),
                    "cost_centre_id": cc_id,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="tests")

    async with S() as session:
        rows = await svc.pl_by_segment(
            session, cid,
            from_date=date(2099, 6, 1),
            to_date=date(2099, 6, 30),
            segment="cost_centre",
        )

    cc_rows = [r for r in rows if r.segment_id == cc_id]
    assert cc_rows, "Cost centre segment not found in report"
    assert "CC-SEG-B" in cc_rows[0].segment_label

    # Cleanup
    async with S() as session:
        entries = (await session.execute(
            select(JournalEntry).where(JournalEntry.description == "cc-segment-test")
        )).scalars().all()
        for e in entries:
            await session.execute(delete(JournalLine).where(JournalLine.entry_id == e.id))
            await session.delete(e)
        from saebooks.models.department import CostCentre as CC
        c = await session.get(CC, cc_id)
        if c:
            await session.delete(c)
        await session.commit()


async def test_pl_by_segment_router_renders(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get(
        "/reports/pl-by-segment?from=2099-01-01&to=2099-12-31"
    )
    assert r.status_code == 200
    assert "P&amp;L by project" in r.text or "P&L by project" in r.text


async def test_pl_by_segment_index_card(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "/reports/pl-by-segment" in r.text


# ---------------------------------------------------------------------- #
# Cleanup                                                                 #
# ---------------------------------------------------------------------- #


@pytest.fixture(autouse=True, scope="module")
async def _cleanup_test_projects() -> AsyncGenerator[None, None]:
    """Purge test projects + sentinel-year journal entries so the
    persistent dev DB doesn't accumulate."""
    yield
    async with AsyncSessionLocal() as session:
        # Delete sentinel-year journal entries first (lines cascade).
        entries = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.description.like("segment-test%"),
                )
            )
        ).scalars().all()
        for e in entries:
            # Lines cascade on entry delete; but because `project_id` is
            # SET NULL we must null those first if we want the projects
            # to delete cleanly.
            await session.execute(
                delete(JournalLine).where(JournalLine.entry_id == e.id)
            )
            await session.delete(e)
        # Then projects
        result = await session.execute(
            select(Project).where(Project.code.like(f"{TEST_PROJECT_PREFIX}-%"))
        )
        for p in result.scalars().all():
            await session.delete(p)
        await session.commit()
