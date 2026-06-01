"""Regression tests for ``persist_bank_lines`` intra-batch dedup.

Root cause (fixed): the dedup added every emitted fingerprint to a
``seen`` set and skipped subsequent matches, so legitimately-distinct
bank lines that happened to share (date, amount, description) — common
in Westpac CSV exports, which carry no per-line reference — collapsed
into a single row. A real $50 was lost when three identical $25 iiNet
refunds on one day folded into one.

The fix introduces an intra-batch occurrence index: the Nth occurrence
of a base fingerprint within a batch gets ``:nN`` appended to its
external_id (1st keeps the bare fingerprint). Re-importing the same file
replays the same occurrence sequence, producing identical external_ids,
so it stays idempotent (zero new rows). Distinct collisions get distinct
external_ids and are all preserved.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.company import Company
from saebooks.services.imports.bank_csv import ParsedLine
from saebooks.services.imports.persist import persist_bank_lines

pytestmark = pytest.mark.postgres_only


async def _primary_company() -> Company:
    async with AsyncSessionLocal() as session:
        primary = (
            await session.execute(
                select(Company)
                .where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                )
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert primary is not None
    return primary


async def _fresh_bank_account(company_id: uuid.UUID) -> Account:
    """A brand-new bank account so line counts are isolated per test."""
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            code=f"DEDUP-{uuid.uuid4().hex[:8].upper()}",
            name="Import-dedup test bank",
            account_type=AccountType.ASSET,
            reconcile=True,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
    return acct


async def _line_count(account_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count(BankStatementLine.id)).where(
                        BankStatementLine.account_id == account_id
                    )
                )
            ).scalar_one()
        )


def _iinet_refund() -> ParsedLine:
    """One of the three identical $25 iiNet refunds on 02/02/2026.

    No reference (Westpac CSV has none) → identical base fingerprint.
    """
    return ParsedLine(
        txn_date=_dt.date(2026, 2, 2),
        amount=Decimal("25.00"),
        description="iiNet Refund",
        reference=None,
    )


async def test_three_identical_lines_persist_three_rows() -> None:
    """The exact $50-loss scenario: 3 identical lines must all persist."""
    company = await _primary_company()
    acct = await _fresh_bank_account(company.id)
    batch = [_iinet_refund(), _iinet_refund(), _iinet_refund()]

    async with AsyncSessionLocal() as session:
        inserted = await persist_bank_lines(
            session, company_id=company.id, account_id=acct.id, lines=batch
        )
        await session.commit()

    assert inserted == 3
    assert await _line_count(acct.id) == 3


async def test_reimport_same_batch_is_idempotent() -> None:
    """Re-running the same batch adds zero rows (same occurrence sequence)."""
    company = await _primary_company()
    acct = await _fresh_bank_account(company.id)
    batch = [_iinet_refund(), _iinet_refund(), _iinet_refund()]

    async with AsyncSessionLocal() as session:
        first = await persist_bank_lines(
            session, company_id=company.id, account_id=acct.id, lines=batch
        )
        await session.commit()
    assert first == 3

    # Re-import the identical file.
    async with AsyncSessionLocal() as session:
        second = await persist_bank_lines(
            session, company_id=company.id, account_id=acct.id, lines=batch
        )
        await session.commit()

    assert second == 0
    assert await _line_count(acct.id) == 3


async def test_two_genuinely_different_lines_both_persist() -> None:
    """Distinct (amount/description) lines remain distinct, as before."""
    company = await _primary_company()
    acct = await _fresh_bank_account(company.id)
    batch = [
        ParsedLine(
            txn_date=_dt.date(2026, 2, 2),
            amount=Decimal("25.00"),
            description="iiNet Refund",
        ),
        ParsedLine(
            txn_date=_dt.date(2026, 2, 2),
            amount=Decimal("-42.50"),
            description="Coles Supermarkets",
        ),
    ]

    async with AsyncSessionLocal() as session:
        inserted = await persist_bank_lines(
            session, company_id=company.id, account_id=acct.id, lines=batch
        )
        await session.commit()

    assert inserted == 2
    assert await _line_count(acct.id) == 2
