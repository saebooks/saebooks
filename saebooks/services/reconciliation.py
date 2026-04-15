"""Bank reconciliation service.

Handles importing statement lines, matching them to posted journal
entries, and unmatching.
"""
import csv
import io
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine


async def bank_accounts(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Account]:
    """Return bank/cash accounts (asset type with reconcile=True)."""
    stmt = (
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.account_type == AccountType.ASSET,
            Account.reconcile.is_(True),
            Account.archived_at.is_(None),
        )
        .order_by(Account.code)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def import_csv(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    csv_text: str,
) -> int:
    """Import bank statement lines from CSV. Returns count of lines imported.

    Expected CSV columns: date, description, amount[, reference]
    Date format: YYYY-MM-DD or DD/MM/YYYY
    Amount: positive=deposit, negative=withdrawal
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    count = 0

    for row in reader:
        # Normalize column names to lowercase, strip whitespace
        norm = {k.strip().lower(): v.strip() for k, v in row.items() if k}

        raw_date = norm.get("date", "")
        raw_desc = norm.get("description", norm.get("desc", norm.get("memo", "")))
        raw_amount = norm.get("amount", "")
        raw_ref = norm.get("reference", norm.get("ref", ""))

        if not raw_date or not raw_amount:
            continue

        txn_date = _parse_date(raw_date)
        if txn_date is None:
            continue

        try:
            amount = Decimal(raw_amount.replace(",", ""))
        except InvalidOperation:
            continue

        line = BankStatementLine(
            company_id=company_id,
            account_id=account_id,
            txn_date=txn_date,
            description=raw_desc or None,
            amount=amount,
            reference=raw_ref or None,
        )
        session.add(line)
        count += 1

    await session.commit()
    return count


async def statement_lines(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: StatementLineStatus | None = None,
) -> list[BankStatementLine]:
    """Get statement lines for a bank account, optionally filtered by status."""
    conditions = [
        BankStatementLine.company_id == company_id,
        BankStatementLine.account_id == account_id,
    ]
    if status:
        conditions.append(BankStatementLine.status == status)

    stmt = (
        select(BankStatementLine)
        .where(and_(*conditions))
        .order_by(BankStatementLine.txn_date, BankStatementLine.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def candidate_entries(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    stmt_line: BankStatementLine,
) -> list[JournalEntry]:
    """Find posted journal entries that could match a statement line.

    Looks for posted entries with a line hitting the bank account
    where the net amount matches the statement line amount.
    """
    # A deposit (positive amount) means we expect a debit to the bank account
    # A withdrawal (negative amount) means we expect a credit to the bank account
    if stmt_line.amount >= 0:
        amount_filter = JournalLine.debit == abs(stmt_line.amount)
    else:
        amount_filter = JournalLine.credit == abs(stmt_line.amount)

    stmt = (
        select(JournalEntry)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.status == EntryStatus.POSTED,
            JournalLine.account_id == account_id,
            amount_filter,
        )
        .order_by(JournalEntry.entry_date)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def match_line(
    session: AsyncSession,
    line_id: uuid.UUID,
    entry_id: uuid.UUID,
) -> BankStatementLine:
    """Match a statement line to a journal entry."""
    stmt_line = await session.get(BankStatementLine, line_id)
    if stmt_line is None:
        raise ValueError("Statement line not found")

    entry = await session.get(JournalEntry, entry_id)
    if entry is None:
        raise ValueError("Journal entry not found")
    if entry.status != EntryStatus.POSTED:
        raise ValueError("Can only match against posted entries")

    stmt_line.matched_entry_id = entry_id
    stmt_line.status = StatementLineStatus.MATCHED
    stmt_line.matched_at = datetime.now()
    stmt_line.matched_by = "admin"

    await session.commit()
    await session.refresh(stmt_line)
    return stmt_line


async def unmatch_line(
    session: AsyncSession,
    line_id: uuid.UUID,
) -> BankStatementLine:
    """Remove match from a statement line."""
    stmt_line = await session.get(BankStatementLine, line_id)
    if stmt_line is None:
        raise ValueError("Statement line not found")

    stmt_line.matched_entry_id = None
    stmt_line.status = StatementLineStatus.UNMATCHED
    stmt_line.matched_at = None
    stmt_line.matched_by = None

    await session.commit()
    await session.refresh(stmt_line)
    return stmt_line


def _parse_date(raw: str) -> date | None:
    """Parse date string in YYYY-MM-DD or DD/MM/YYYY format."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
