"""Regression tests for migration 0101 — POSTED-JE balance + line-count trigger.

Pins the DB-level invariant the audit's CRITICAL #1 was about: a
service that bypasses ``saebooks.services.journal.post`` cannot
commit an unbalanced or single-line POSTED journal entry.

Each test drops to raw SQL because the *point* is that going around
the service layer should still fail. If we asserted via the service,
we'd be testing the helper, not the trigger.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
pytestmark = pytest.mark.postgres_only


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, tenant_id, debit_account_id, credit_account_id)."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        accts = (
            await session.execute(
                select(Account)
                .where(Account.company_id == co.id)
                .order_by(Account.code)
                .limit(2)
            )
        ).scalars().all()
        return co.id, co.tenant_id, accts[0].id, accts[1].id


async def _insert_je_raw(
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str,
    lines: list[tuple[uuid.UUID, Decimal, Decimal]],
) -> uuid.UUID:
    """INSERT a JE + lines via raw SQL, bypassing the service layer."""
    je_id = uuid.uuid4()
    ref = f"TRG-{je_id.hex[:8]}"
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO journal_entries
                    (id, company_id, tenant_id, ref, entry_date,
                     description, status, version)
                VALUES
                    (:id, :cid, :tid, :ref, '2026-05-10',
                     'trigger test', :status, 1)
                """
            ),
            {
                "id": je_id,
                "cid": company_id,
                "tid": tenant_id,
                "ref": ref,
                "status": status,
            },
        )
        for line_no, (acct, debit, credit) in enumerate(lines, start=1):
            await session.execute(
                text(
                    """
                    INSERT INTO journal_lines
                        (id, entry_id, line_no, account_id, debit, credit)
                    VALUES
                        (:id, :entry, :ln, :acct, :debit, :credit)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "entry": je_id,
                    "ln": line_no,
                    "acct": acct,
                    "debit": debit,
                    "credit": credit,
                },
            )
        await session.commit()
    return je_id


@pytest.mark.asyncio
async def test_draft_unbalanced_is_allowed() -> None:
    """DRAFT entries are explicitly outside the trigger's scope."""
    company_id, tenant_id, a, b = await _ctx()
    je_id = await _insert_je_raw(
        company_id,
        tenant_id,
        status="DRAFT",
        lines=[(a, Decimal("100"), Decimal("0"))],
    )
    # Cleanup — DELETE will not trigger the check because the JE is
    # DRAFT, but the constraint trigger still runs for the deferred
    # path. The function returns early on DRAFT.
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM journal_lines WHERE entry_id = :id"),
            {"id": je_id},
        )
        await s.execute(
            text("DELETE FROM journal_entries WHERE id = :id"),
            {"id": je_id},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_posted_unbalanced_rejected() -> None:
    """A POSTED entry whose debits != credits must be rejected at COMMIT."""
    company_id, tenant_id, a, b = await _ctx()
    with pytest.raises(IntegrityError, match="unbalanced"):
        await _insert_je_raw(
            company_id,
            tenant_id,
            status="POSTED",
            lines=[
                (a, Decimal("100"), Decimal("0")),
                (b, Decimal("0"), Decimal("99")),  # 1c short
            ],
        )


@pytest.mark.asyncio
async def test_posted_single_line_rejected() -> None:
    """A POSTED entry with only one line must be rejected at COMMIT."""
    company_id, tenant_id, a, _ = await _ctx()
    with pytest.raises(IntegrityError, match="minimum 2 required"):
        await _insert_je_raw(
            company_id,
            tenant_id,
            status="POSTED",
            lines=[(a, Decimal("100"), Decimal("100"))],
        )


@pytest.mark.asyncio
async def test_posted_balanced_allowed() -> None:
    """The happy path — balanced + 2+ lines passes."""
    company_id, tenant_id, a, b = await _ctx()
    je_id = await _insert_je_raw(
        company_id,
        tenant_id,
        status="POSTED",
        lines=[
            (a, Decimal("100"), Decimal("0")),
            (b, Decimal("0"), Decimal("100")),
        ],
    )
    # Tear down — but the trigger now also fires on DELETE-on-lines,
    # so the JE row must remain intact while we strip lines. Easier
    # to demote to DRAFT first, which exits the function early.
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE journal_entries SET status = 'DRAFT' WHERE id = :id"
            ),
            {"id": je_id},
        )
        await s.execute(
            text("DELETE FROM journal_lines WHERE entry_id = :id"),
            {"id": je_id},
        )
        await s.execute(
            text("DELETE FROM journal_entries WHERE id = :id"),
            {"id": je_id},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_post_via_status_update_validates() -> None:
    """A DRAFT made unbalanced cannot be flipped to POSTED."""
    company_id, tenant_id, a, b = await _ctx()
    je_id = await _insert_je_raw(
        company_id,
        tenant_id,
        status="DRAFT",
        lines=[
            (a, Decimal("100"), Decimal("0")),
            (b, Decimal("0"), Decimal("50")),  # unbalanced draft
        ],
    )
    try:
        with pytest.raises(IntegrityError, match="unbalanced"):
            async with AsyncSessionLocal() as s:
                await s.execute(
                    text(
                        "UPDATE journal_entries SET status = 'POSTED' "
                        "WHERE id = :id"
                    ),
                    {"id": je_id},
                )
                await s.commit()
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("DELETE FROM journal_lines WHERE entry_id = :id"),
                {"id": je_id},
            )
            await s.execute(
                text("DELETE FROM journal_entries WHERE id = :id"),
                {"id": je_id},
            )
            await s.commit()
