"""Bank-feeds reconciliation sweep (Batch HH).

Walks every active :class:`BankFeedAccount` for a company and flags:

1. **Staleness** — last imported statement line older than 7 days
2. **Unmatched count** — number of ``UNMATCHED`` statement lines still
   awaiting a manual match or a journal entry
3. **Variance** — sum of statement lines (what the feed says came in/out)
   vs sum of posted journal-line movement on the linked GL account. A
   variance >$0.01 means GL and feed have diverged — either a manual
   journal was posted that the feed never saw, or vice versa.

The logic is intentionally offline: we don't call SISS. Everything is
computed from rows we already have. Runs weekly from cron alongside the
daily sync; see ``saebooks.cli reconcile-feeds``.

The UI surfaces this at ``/admin/bank-feeds/health`` as a table of
per-account health (red if stale or variance >$0.01, amber if unmatched
lines, green otherwise).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

logger = logging.getLogger(__name__)

# How many days of silence before we flag a feed as stale. Banks usually
# post overnight; 7 days covers a long weekend + holiday stacking.
DEFAULT_STALE_DAYS = 7

# Variance tolerance — anything under this is rounding noise, above is
# a real issue worth surfacing to the admin.
VARIANCE_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class AccountHealth:
    """Per-account variance + staleness snapshot."""

    bank_feed_account_id: uuid.UUID
    ledger_account_id: uuid.UUID
    ledger_account_code: str
    ledger_account_name: str
    display_name: str | None
    masked_number: str | None
    last_statement_date: date | None
    days_since_last_statement: int | None
    stale: bool
    unmatched_count: int
    feed_total: Decimal  # sum of all statement_lines.amount for this feed
    gl_total: Decimal    # sum of journal_lines (debit-credit) on ledger account
    variance: Decimal    # feed_total - gl_total
    has_variance: bool   # |variance| > VARIANCE_TOLERANCE

    @property
    def severity(self) -> str:
        """One-word rollup for the UI: ``ok``, ``warn``, or ``error``."""

        if self.stale or self.has_variance:
            return "error"
        if self.unmatched_count:
            return "warn"
        return "ok"


@dataclass(frozen=True)
class ReconciliationReport:
    """Sweep result for one company."""

    company_id: uuid.UUID
    through_date: date
    accounts: list[AccountHealth] = field(default_factory=list)

    @property
    def total_variance(self) -> Decimal:
        return sum((a.variance for a in self.accounts), start=Decimal("0"))

    @property
    def has_any_issue(self) -> bool:
        return any(a.severity != "ok" for a in self.accounts)

    @property
    def worst_severity(self) -> str:
        if any(a.severity == "error" for a in self.accounts):
            return "error"
        if any(a.severity == "warn" for a in self.accounts):
            return "warn"
        return "ok"


async def _statement_totals(
    session: AsyncSession, bank_feed_account_id: uuid.UUID
) -> tuple[Decimal, int, date | None]:
    """Return (feed_total, unmatched_count, last_txn_date) for one feed."""

    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(BankStatementLine.amount), Decimal("0")
                ),
                func.count(BankStatementLine.id),
                func.max(BankStatementLine.txn_date),
            ).where(
                BankStatementLine.bank_feed_account_id == bank_feed_account_id
            )
        )
    ).one()
    feed_total = Decimal(row[0] or 0)

    unmatched = (
        await session.execute(
            select(func.count(BankStatementLine.id)).where(
                BankStatementLine.bank_feed_account_id == bank_feed_account_id,
                BankStatementLine.status == StatementLineStatus.UNMATCHED.value,
            )
        )
    ).scalar_one()

    last_txn = row[2]
    return feed_total, int(unmatched or 0), last_txn


async def _ledger_total(
    session: AsyncSession, ledger_account_id: uuid.UUID
) -> Decimal:
    """Sum posted journal-line movement on this GL account.

    ``debit - credit`` so the sign matches the bank-statement convention
    (positive = money into the bank account, negative = money out).
    Only :attr:`EntryStatus.POSTED` entries count — draft/voided are
    excluded.
    """

    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(JournalLine.debit - JournalLine.credit),
                    Decimal("0"),
                )
            )
            .select_from(JournalLine)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                JournalLine.account_id == ledger_account_id,
                JournalEntry.status == EntryStatus.POSTED.value,
            )
        )
    ).scalar_one()
    return Decimal(row or 0)


async def sweep(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    through_date: date | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> ReconciliationReport:
    """Run the reconciliation sweep for one company.

    Pure read-only — does not write anything. Caller decides what to do
    with the report (render it, email it, exit non-zero, etc.).
    """

    if through_date is None:
        through_date = date.today()

    feed_rows = (
        await session.execute(
            select(BankFeedAccount, Account)
            .join(Account, Account.id == BankFeedAccount.ledger_account_id)
            .where(
                BankFeedAccount.company_id == company_id,
                BankFeedAccount.revoked_at.is_(None),
            )
            .order_by(Account.code)
        )
    ).all()

    accounts: list[AccountHealth] = []
    for feed, ledger in feed_rows:
        feed_total, unmatched, last_txn = await _statement_totals(
            session, feed.id
        )
        gl_total = await _ledger_total(session, ledger.id)
        variance = feed_total - gl_total

        days_since = None
        stale = False
        if last_txn is not None:
            days_since = (through_date - last_txn).days
            stale = days_since > stale_days
        else:
            # No statement lines ever imported — definitely stale if
            # the feed has been around more than `stale_days`.
            age = (
                datetime.now(UTC) - feed.created_at
            ).days
            stale = age > stale_days

        accounts.append(
            AccountHealth(
                bank_feed_account_id=feed.id,
                ledger_account_id=ledger.id,
                ledger_account_code=ledger.code,
                ledger_account_name=ledger.name,
                display_name=feed.display_name,
                masked_number=feed.masked_number,
                last_statement_date=last_txn,
                days_since_last_statement=days_since,
                stale=stale,
                unmatched_count=unmatched,
                feed_total=feed_total.quantize(Decimal("0.01")),
                gl_total=gl_total.quantize(Decimal("0.01")),
                variance=variance.quantize(Decimal("0.01")),
                has_variance=abs(variance) > VARIANCE_TOLERANCE,
            )
        )

    return ReconciliationReport(
        company_id=company_id,
        through_date=through_date,
        accounts=accounts,
    )


__all__ = [
    "DEFAULT_STALE_DAYS",
    "VARIANCE_TOLERANCE",
    "AccountHealth",
    "ReconciliationReport",
    "sweep",
]


def _fmt_report_line(a: AccountHealth) -> str:
    """Formatter used by the CLI for structured per-account logging."""

    bits = [
        f"account={a.ledger_account_code}",
        f"feed_total={a.feed_total}",
        f"gl_total={a.gl_total}",
        f"variance={a.variance}",
        f"unmatched={a.unmatched_count}",
        f"stale={a.stale}",
        f"severity={a.severity}",
    ]
    if a.last_statement_date is not None:
        bits.append(f"last_txn={a.last_statement_date.isoformat()}")
    return " ".join(bits)


async def sweep_all_companies(
    session: AsyncSession,
    *,
    through_date: date | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> list[ReconciliationReport]:
    """Run :func:`sweep` for every company that has any active feed.

    Used by the CLI entry point — not currently called from the UI.
    """

    distinct_company_ids = (
        await session.execute(
            select(BankFeedAccount.company_id)
            .where(BankFeedAccount.revoked_at.is_(None))
            .distinct()
        )
    ).scalars().all()

    reports: list[ReconciliationReport] = []
    for cid in distinct_company_ids:
        reports.append(
            await sweep(
                session,
                company_id=cid,
                through_date=through_date,
                stale_days=stale_days,
            )
        )
    return reports


def stale_cutoff(through_date: date, stale_days: int = DEFAULT_STALE_DAYS) -> date:
    """Cutoff date: any last-txn older than this is stale.

    Tiny pure helper so the UI can render "stale if older than <date>"
    alongside the table without re-doing the arithmetic.
    """

    return through_date - timedelta(days=stale_days)
