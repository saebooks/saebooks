"""Web regression: post an auto-reversing accrual from the GUI.

Pins Phase-1 item #4 — a single ``POST /journal/save`` with
``action=post`` + ``auto_reverse=1`` + ``reverse_date`` must book BOTH
the accrual entry (on ``entry_date``) and its mirror reversal (on
``reverse_date``). This is the period-end "$2,720 30-Jun accrual that
auto-reverses 1-Jul" workflow done in the app rather than via raw SQL.
"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry

pytestmark = pytest.mark.postgres_only


async def _company_and_two_leaves() -> tuple[Company, str, str]:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        leaves = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == co.id,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
                .limit(2)
            )
        ).scalars().all()
        assert len(leaves) == 2, "need two postable leaf accounts in the seed CoA"
        return co, str(leaves[0].id), str(leaves[1].id)


@pytest.mark.asyncio
async def test_post_with_auto_reverse_books_both_legs(admin_client: AsyncClient) -> None:
    co, acct_a, acct_b = await _company_and_two_leaves()

    r = await admin_client.post(
        "/journal/save",
        data={
            "entry_date": "2025-06-30",
            "description": "Auto-reverse web test accrual",
            "ref": "",
            "action": "post",
            "auto_reverse": "1",
            "override_reason": "year-end accrual (test)",
            "reverse_date": "2025-07-01",
            "line_0_account_id": acct_a,
            "line_0_description": "accrual Dr",
            "line_0_debit": "100.00",
            "line_0_credit": "0",
            "line_1_account_id": acct_b,
            "line_1_description": "accrual Cr",
            "line_1_debit": "0",
            "line_1_credit": "100.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    async with AsyncSessionLocal() as session:
        # The posted accrual lands on 30-Jun.
        accrual = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == co.id,
                    JournalEntry.entry_date == date(2025, 6, 30),
                    JournalEntry.description == "Auto-reverse web test accrual",
                )
            )
        ).scalars().first()
        assert accrual is not None, "accrual entry was not posted"

        # Its mirror reversal lands on 1-Jul and points back at it.
        reversal = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.reversal_of_id == accrual.id,
                )
            )
        ).scalars().first()
        assert reversal is not None, "auto-reversal entry was not created"
        assert reversal.entry_date == date(2025, 7, 1)
        assert reversal.status == EntryStatus.POSTED
