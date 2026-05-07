"""Trust account reports — NSW Property and Stock Agents Act 2002.

Two reports for real-estate agency trust compliance:

  trust_cashbook          — receipts & payments cash book for trust bank accounts
  unreconciled_trust_balances — liability balance owed to beneficiaries

Both operate only on POSTED journal entries.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine


@dataclass
class TrustCashbookLine:
    entry_id: uuid.UUID
    entry_date: date
    ref: str
    description: str
    receipts: Decimal
    payments: Decimal
    running_balance: Decimal


@dataclass
class TrustCashbookReport:
    account_name: str
    from_date: date | None
    to_date: date | None
    opening_balance: Decimal
    lines: list[TrustCashbookLine] = field(default_factory=list)
    closing_balance: Decimal = Decimal("0")
    total_receipts: Decimal = Decimal("0")
    total_payments: Decimal = Decimal("0")


@dataclass
class TrustBalanceLine:
    account_code: str
    account_name: str
    balance: Decimal


@dataclass
class TrustBalancesReport:
    as_of: date
    lines: list[TrustBalanceLine] = field(default_factory=list)
    total_balance: Decimal = Decimal("0")


async def trust_cashbook(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[TrustCashbookReport]:
    """Return a cash book for each trust bank account (is_trust_account=True, reconcile=True).

    Each report lists receipts (debit on trust bank) and payments (credit
    on trust bank) in the given period, with opening balance and running total.
    NSW agencies must print and sign this report monthly (PSAA 2002 s.105).
    """
    trust_accts = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.is_trust_account.is_(True),
                Account.reconcile.is_(True),
                Account.archived_at.is_(None),
            )
            .order_by(Account.code)
        )
    ).scalars().all()

    if not trust_accts:
        return []

    reports: list[TrustCashbookReport] = []
    for acct in trust_accts:
        # Opening balance: sum of all posted lines before from_date
        opening = Decimal("0")
        if from_date:
            pre_rows = (
                await session.execute(
                    select(JournalLine.debit, JournalLine.credit)
                    .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                    .where(
                        JournalEntry.company_id == company_id,
                        JournalEntry.status == EntryStatus.POSTED,
                        JournalLine.account_id == acct.id,
                        JournalEntry.entry_date < from_date,
                    )
                )
            ).all()
            for dr, cr in pre_rows:
                opening += dr - cr

        # Period lines
        period_q = (
            select(JournalLine, JournalEntry)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalLine.account_id == acct.id,
            )
            .order_by(JournalEntry.entry_date, JournalEntry.ref, JournalLine.line_no)
        )
        if from_date:
            period_q = period_q.where(JournalEntry.entry_date >= from_date)
        if to_date:
            period_q = period_q.where(JournalEntry.entry_date <= to_date)

        rows = (await session.execute(period_q)).all()

        running = opening
        total_receipts = Decimal("0")
        total_payments = Decimal("0")
        lines: list[TrustCashbookLine] = []
        for ln, entry in rows:
            running += ln.debit - ln.credit
            total_receipts += ln.debit
            total_payments += ln.credit
            lines.append(
                TrustCashbookLine(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    ref=entry.ref,
                    description=ln.description or entry.description or "",
                    receipts=ln.debit,
                    payments=ln.credit,
                    running_balance=running,
                )
            )

        reports.append(
            TrustCashbookReport(
                account_name=acct.name,
                from_date=from_date,
                to_date=to_date,
                opening_balance=opening,
                lines=lines,
                closing_balance=running,
                total_receipts=total_receipts,
                total_payments=total_payments,
            )
        )

    return reports


async def unreconciled_trust_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> TrustBalancesReport:
    """Return the unreconciled trust liability balance per account.

    Finds LIABILITY accounts that appear in the same posted journal entries
    as a trust bank account line.  The credit balance of those accounts
    represents funds held in trust on behalf of beneficiaries
    (landlords / vendors under management).
    """
    as_of_date = as_of or date.today()

    # Identify trust bank account IDs
    trust_acct_ids = {
        row[0]
        for row in (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == company_id,
                    Account.is_trust_account.is_(True),
                    Account.reconcile.is_(True),
                    Account.archived_at.is_(None),
                )
            )
        ).all()
    }

    if not trust_acct_ids:
        return TrustBalancesReport(as_of=as_of_date)

    # Journal entries that touch a trust bank account on or before as_of
    trust_entry_ids = {
        row[0]
        for row in (
            await session.execute(
                select(JournalLine.entry_id)
                .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                .where(
                    JournalEntry.company_id == company_id,
                    JournalEntry.status == EntryStatus.POSTED,
                    JournalLine.account_id.in_(trust_acct_ids),
                    JournalEntry.entry_date <= as_of_date,
                )
                .distinct()
            )
        ).all()
    }

    if not trust_entry_ids:
        return TrustBalancesReport(as_of=as_of_date)

    # Balance of LIABILITY accounts that appear in those entries
    rows = (
        await session.execute(
            select(Account.code, Account.name, JournalLine.debit, JournalLine.credit)
            .join(Account, JournalLine.account_id == Account.id)
            .where(
                JournalLine.entry_id.in_(trust_entry_ids),
                Account.account_type == AccountType.LIABILITY,
                JournalLine.account_id.not_in(trust_acct_ids),
            )
            .order_by(Account.code)
        )
    ).all()

    balances: dict[tuple[str, str], Decimal] = {}
    for code, name, debit, credit in rows:
        key = (code, name)
        # LIABILITY normal balance is credit; balance = credit - debit
        balances[key] = balances.get(key, Decimal("0")) + credit - debit

    lines = [
        TrustBalanceLine(account_code=code, account_name=name, balance=bal)
        for (code, name), bal in sorted(balances.items())
        if bal != Decimal("0")
    ]
    total = sum((ln.balance for ln in lines), Decimal("0"))

    return TrustBalancesReport(as_of=as_of_date, lines=lines, total_balance=total)
