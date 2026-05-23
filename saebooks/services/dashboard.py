"""Dashboard service — read-only aggregations for the landing page.

The dashboard widgets want answers like "what's in the bank" and
"how many unmatched lines are waiting", not whole reports. Each
function here returns a small dataclass the router hands straight
to a Jinja widget include; no business logic in templates.

All functions are **read-only** and **safe on an empty DB** —
every widget returns a well-formed empty struct if there's nothing
to show, so the router never has to branch on missing data.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.recurring_invoice import (
    RecurrenceStatus,
    RecurringInvoice,
)
from saebooks.services import reports as reports_svc

# ---------------------------------------------------------------------- #
# Bank balances                                                           #
# ---------------------------------------------------------------------- #


@dataclass
class BankBalance:
    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    balance: Decimal  # GL debit - credit; negative on a LIABILITY card = owed to bank


async def bank_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> list[BankBalance]:
    """GL balance per reconcilable bank/cash/credit-card account.

    A "bank account" here is the same definition used by the
    reconciliation service: ``account_type IN (ASSET, LIABILITY)`` +
    ``reconcile=True`` + not archived. ASSET covers cheque/savings/cash/
    undeposited-funds; LIABILITY covers credit cards. Balance is
    cumulative POSTED journal lines (debit − credit) up to ``as_of``
    (default today), so a credit card with money owed reads negative.
    """
    cutoff = as_of or date.today()

    stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .select_from(Account)
        .outerjoin(JournalLine, JournalLine.account_id == Account.id)
        .outerjoin(
            JournalEntry,
            and_(
                JournalEntry.id == JournalLine.entry_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.entry_date <= cutoff,
            ),
        )
        .where(
            Account.company_id == company_id,
            Account.account_type.in_((AccountType.ASSET, AccountType.LIABILITY)),
            Account.reconcile.is_(True),
            Account.archived_at.is_(None),
        )
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )

    rows = (await session.execute(stmt)).all()
    balances: list[BankBalance] = []
    for acct_id, code, name, acct_type, dr, cr in rows:
        # Filter on JournalEntry.status lands in the outer-join
        # condition so unposted/voided entries don't contribute.
        # Rows with no matched journal lines still appear with
        # balance 0 — empty-state friendly.
        balances.append(
            BankBalance(
                account_id=acct_id,
                code=code,
                name=name,
                account_type=acct_type,
                balance=Decimal(dr) - Decimal(cr),
            )
        )
    return balances


# ---------------------------------------------------------------------- #
# Aged-AR snapshot                                                        #
# ---------------------------------------------------------------------- #


@dataclass
class AgedArSnapshot:
    """Grand-total bucket roll-up from the aged-AR report."""

    current: Decimal = Decimal("0")
    d1_30: Decimal = Decimal("0")
    d31_60: Decimal = Decimal("0")
    d61_90: Decimal = Decimal("0")
    d90_plus: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return (
            self.current + self.d1_30 + self.d31_60 + self.d61_90 + self.d90_plus
        )

    @property
    def overdue(self) -> Decimal:
        return self.d1_30 + self.d31_60 + self.d61_90 + self.d90_plus


async def aged_ar_snapshot(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_at: date | None = None,
) -> AgedArSnapshot:
    """Wraps ``reports.aged_ar`` and returns just the grand totals.

    Uses the same bucket keys + age math as the full report, so
    dashboard and /reports/aged-ar can never disagree.
    """
    report = await reports_svc.aged_ar(session, company_id, as_at=as_at)
    gt = report.grand_totals
    return AgedArSnapshot(
        current=gt["current"],
        d1_30=gt["d1_30"],
        d31_60=gt["d31_60"],
        d61_90=gt["d61_90"],
        d90_plus=gt["d90_plus"],
    )


# ---------------------------------------------------------------------- #
# Unmatched statement line count                                          #
# ---------------------------------------------------------------------- #


async def unmatched_statement_lines_count(
    session: AsyncSession, company_id: uuid.UUID
) -> int:
    """Count of bank-statement lines still awaiting a match.

    Pure count (no sum) — the headline number belongs on the chip,
    dollar total lives inside the reconciliation page.
    """
    stmt = (
        select(func.count(BankStatementLine.id))
        .where(
            BankStatementLine.company_id == company_id,
            BankStatementLine.status == StatementLineStatus.UNMATCHED,
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


# ---------------------------------------------------------------------- #
# 30-day cashflow sparkline                                               #
# ---------------------------------------------------------------------- #


@dataclass
class CashflowSparkline:
    """One point per day across the last N days.

    ``points`` is a list of ``(day, net_amount)`` tuples, sorted
    ascending. ``net_amount`` is the net movement on reconcilable
    bank accounts that day (positive = inflow).
    """

    days: int
    points: list[tuple[date, Decimal]] = field(default_factory=list)

    @property
    def max_abs(self) -> Decimal:
        """Peak magnitude across the series — used to scale the SVG Y axis."""
        if not self.points:
            return Decimal("0")
        return max(abs(p[1]) for p in self.points)


async def cashflow_30d(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
    days: int = 30,
) -> CashflowSparkline:
    """Net daily cash movement across every reconcilable asset account.

    Sums ``debit - credit`` per day on POSTED journal lines that
    touch an ``ASSET`` + ``reconcile=True`` account. Zero-fills
    days with no activity so the sparkline is the full ``days``
    wide regardless of DB density.
    """
    end = as_of or date.today()
    start = end - timedelta(days=days - 1)

    stmt = (
        select(
            JournalEntry.entry_date,
            func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0),
        )
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.status == EntryStatus.POSTED,
            JournalEntry.entry_date >= start,
            JournalEntry.entry_date <= end,
            Account.account_type == AccountType.ASSET,
            Account.reconcile.is_(True),
            Account.archived_at.is_(None),
        )
        .group_by(JournalEntry.entry_date)
    )

    rows = (await session.execute(stmt)).all()
    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for day, net in rows:
        by_day[day] = Decimal(net)

    points: list[tuple[date, Decimal]] = []
    for i in range(days):
        d = start + timedelta(days=i)
        points.append((d, by_day[d]))
    return CashflowSparkline(days=days, points=points)


def sparkline_svg(
    cashflow: CashflowSparkline,
    *,
    width: int = 300,
    height: int = 60,
    stroke: str = "currentColor",
) -> str:
    """Render the sparkline as an inline SVG polyline string.

    Rendering-in-the-service keeps the template dumb (just ``{{ svg|safe }}``)
    and lets this function be unit-tested without a browser. No JS lib,
    no external fetch.
    """
    points = cashflow.points
    if not points:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-label="No cashflow data"></svg>'
        )

    max_abs = cashflow.max_abs
    # Avoid divide-by-zero when all movement is zero — collapse to a
    # flat line in the middle.
    if max_abs == 0:
        max_abs = Decimal("1")

    mid = height / 2
    # Leave a 2px margin on each vertical edge.
    amp = (height - 4) / 2
    step = width / max(len(points) - 1, 1)

    coords: list[str] = []
    for i, (_, value) in enumerate(points):
        x = i * step
        # SVG y grows downward, so invert.
        y = mid - float(value) / float(max_abs) * amp
        coords.append(f"{x:.1f},{y:.1f}")

    zero_y = mid  # baseline
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="30-day cashflow">'
        f'<line x1="0" y1="{zero_y}" x2="{width}" y2="{zero_y}" '
        f'stroke="#ccc" stroke-width="1" stroke-dasharray="2,2"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" '
        f'stroke="{stroke}" stroke-width="1.5"/>'
        f"</svg>"
    )


# ---------------------------------------------------------------------- #
# Upcoming recurring invoices                                             #
# ---------------------------------------------------------------------- #


@dataclass
class UpcomingRecurring:
    template_id: uuid.UUID
    name: str
    contact_id: uuid.UUID
    next_run: date
    frequency: str


async def upcoming_recurring(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    limit: int = 5,
) -> list[UpcomingRecurring]:
    """Next N ACTIVE recurring-invoice templates by ``next_run`` ascending."""
    stmt = (
        select(RecurringInvoice)
        .where(
            RecurringInvoice.company_id == company_id,
            RecurringInvoice.status == RecurrenceStatus.ACTIVE,
            RecurringInvoice.archived_at.is_(None),
        )
        .order_by(RecurringInvoice.next_run.asc(), RecurringInvoice.created_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        UpcomingRecurring(
            template_id=r.id,
            name=r.name,
            contact_id=r.contact_id,
            next_run=r.next_run,
            frequency=r.frequency.value,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------- #
# Combined bundle (one round-trip into the router)                        #
# ---------------------------------------------------------------------- #


@dataclass
class DashboardBundle:
    bank_balances: list[BankBalance]
    aged_ar: AgedArSnapshot
    unmatched_count: int
    cashflow: CashflowSparkline
    cashflow_svg: str
    upcoming: list[UpcomingRecurring]


async def build_dashboard(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> DashboardBundle:
    """One-shot bundle used by the router.

    Keeps each widget async-calling-awaitable (not gathered — the
    async session isn't safe under concurrent execute — so calls
    are sequential but still under one session). The fan-out is
    cheap enough on a single-company DB that concurrency would
    save a few ms at the cost of a session-lifecycle footgun.
    """
    today = as_of or date.today()

    balances = await bank_balances(session, company_id, as_of=today)
    aged = await aged_ar_snapshot(session, company_id, as_at=today)
    unmatched = await unmatched_statement_lines_count(session, company_id)
    flow = await cashflow_30d(session, company_id, as_of=today)
    svg = sparkline_svg(flow)
    upcoming = await upcoming_recurring(session, company_id)

    return DashboardBundle(
        bank_balances=balances,
        aged_ar=aged,
        unmatched_count=unmatched,
        cashflow=flow,
        cashflow_svg=svg,
        upcoming=upcoming,
    )
