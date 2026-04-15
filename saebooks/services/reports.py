"""Reporting service — trial balance, P&L, balance sheet.

All reports operate on POSTED journal lines only.
"""
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

# Account types that go on the balance sheet (permanent accounts)
BALANCE_SHEET_TYPES = {
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
}

# Account types that go on the P&L (temporary accounts)
PNL_TYPES = {
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
}


@dataclass
class AccountBalance:
    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")

    @property
    def balance(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class ReportSection:
    label: str
    account_type: AccountType
    rows: list[AccountBalance] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((r.debit for r in self.rows), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), Decimal("0"))

    @property
    def total_balance(self) -> Decimal:
        return sum((r.balance for r in self.rows), Decimal("0"))


async def trial_balance(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> list[ReportSection]:
    """Trial balance: sum of debits and credits per account for posted entries."""
    balances = await _account_balances(session, company_id, as_of=as_of)
    return _group_balances(balances)


async def profit_and_loss(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[ReportSection], Decimal]:
    """P&L: income - expenses for a period. Returns (sections, net_profit)."""
    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=to_date
    )
    pnl = [b for b in balances if b.account_type in PNL_TYPES]
    sections = _group_balances(pnl)

    income = sum(
        (s.total_balance for s in sections
         if s.account_type in {AccountType.INCOME, AccountType.OTHER_INCOME}),
        Decimal("0"),
    )
    expenses = sum(
        (s.total_balance for s in sections
         if s.account_type in {
             AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE
         }),
        Decimal("0"),
    )
    # Income is credit-normal (negative balance), expenses debit-normal (positive)
    net_profit = -income - expenses
    return sections, net_profit


async def balance_sheet(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> tuple[list[ReportSection], Decimal]:
    """Balance sheet: assets, liabilities, equity. Returns (sections, net_assets)."""
    balances = await _account_balances(session, company_id, as_of=as_of)
    bs = [b for b in balances if b.account_type in BALANCE_SHEET_TYPES]
    sections = _group_balances(bs)

    assets = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.ASSET),
        Decimal("0"),
    )
    liabilities = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.LIABILITY),
        Decimal("0"),
    )
    equity = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.EQUITY),
        Decimal("0"),
    )
    net_assets = assets + liabilities + equity  # liabilities are credit-normal (negative)
    return sections, net_assets


async def _account_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    as_of: date | None = None,
) -> list[AccountBalance]:
    """Aggregate posted journal lines into per-account debit/credit totals."""
    # Build filters
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status == EntryStatus.POSTED,
    ]
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date or as_of:
        conditions.append(JournalEntry.entry_date <= (to_date or as_of))

    stmt = (
        select(
            JournalLine.account_id,
            Account.code,
            Account.name,
            Account.account_type,
            JournalLine.debit,
            JournalLine.credit,
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(and_(*conditions))
    )

    result = await session.execute(stmt)

    totals: dict[uuid.UUID, AccountBalance] = {}
    for row in result.all():
        acct_id = row[0]
        if acct_id not in totals:
            totals[acct_id] = AccountBalance(
                account_id=acct_id,
                code=row[1],
                name=row[2],
                account_type=row[3],
            )
        totals[acct_id].debit += row[4]
        totals[acct_id].credit += row[5]

    return sorted(totals.values(), key=lambda b: b.code)


TYPE_ORDER = [
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
    AccountType.COST_OF_SALES,
    AccountType.EXPENSE,
    AccountType.OTHER_EXPENSE,
]

TYPE_LABELS = {
    AccountType.ASSET: "Assets",
    AccountType.LIABILITY: "Liabilities",
    AccountType.EQUITY: "Equity",
    AccountType.INCOME: "Income",
    AccountType.OTHER_INCOME: "Other income",
    AccountType.COST_OF_SALES: "Cost of sales",
    AccountType.EXPENSE: "Expenses",
    AccountType.OTHER_EXPENSE: "Other expense",
}


def _group_balances(balances: list[AccountBalance]) -> list[ReportSection]:
    by_type: dict[AccountType, list[AccountBalance]] = defaultdict(list)
    for b in balances:
        if b.debit != Decimal("0") or b.credit != Decimal("0"):
            by_type[b.account_type].append(b)

    sections = []
    for t in TYPE_ORDER:
        if rows := by_type.get(t):
            sections.append(ReportSection(
                label=TYPE_LABELS.get(t, t.value),
                account_type=t,
                rows=rows,
            ))
    return sections
