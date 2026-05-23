"""Bank reconciliation service.

Handles importing statement lines, matching them to posted journal
entries or payments, and unmatching. Supports N:1 matching: a single
bank statement line can be allocated against many targets (e.g. a
Medicare batch EFT covering 30 invoice payments).

Storage model
-------------
* The ``bsl_matches`` junction (migration 0077) is the **source of
  truth** for what's reconciled to what. ``recompute_status`` reads
  this table to decide UNMATCHED / PARTIAL / MATCHED.
* The 1:1 columns on ``bank_statement_lines`` (``matched_entry_id``,
  ``matched_to_type``, ``matched_to_id``, ``matched_at``,
  ``matched_by``) are kept populated for back-compat with existing
  readers. They mirror the most-recent (or, in N:1 cases, the
  largest) match — see ``_sync_legacy_columns``.
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
from saebooks.models.bsl_match import (
    TARGET_JOURNAL_ENTRY,
    TARGET_PAYMENT,
    BslMatch,
)
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.payment import Payment

_VALID_TARGETS = {TARGET_PAYMENT, TARGET_JOURNAL_ENTRY}
_TARGET_MODEL = {TARGET_PAYMENT: Payment, TARGET_JOURNAL_ENTRY: JournalEntry}

# Allocations within this many cents of the BSL amount count as fully
# matched. Two-cent fuzz handles cumulative GST rounding when many
# lines are summed; tighter than that and accountants chase phantom
# pennies, looser and real off-by-a-dollar errors slip through.
_MATCH_TOLERANCE = Decimal("0.02")


async def bank_accounts(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Account]:
    """Return reconcilable bank/cash/credit-card accounts.

    Includes ASSET (cheque/savings/cash/undeposited-funds) and LIABILITY
    (credit cards) with ``reconcile=True`` and not archived. Credit cards
    are reconciled against statement lines the same way bank accounts
    are; the sign on the GL just runs the other way.
    """
    stmt = (
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.account_type.in_((AccountType.ASSET, AccountType.LIABILITY)),
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
    """Import bank statement lines from CSV. Returns count imported."""
    reader = csv.DictReader(io.StringIO(csv_text))
    count = 0

    for row in reader:
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
    """Get statement lines for a bank account, optionally filtered."""
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
    """Find posted journal entries that could match a statement line."""
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


# ---------------------------------------------------------------- #
# Junction-table API (N:1)                                         #
# ---------------------------------------------------------------- #


async def list_matches(
    session: AsyncSession, bsl_id: uuid.UUID
) -> list[BslMatch]:
    """Return non-archived matches for a BSL, oldest first."""
    stmt = (
        select(BslMatch)
        .where(BslMatch.bsl_id == bsl_id, BslMatch.archived_at.is_(None))
        .order_by(BslMatch.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def add_match(
    session: AsyncSession,
    *,
    bsl_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    amount: Decimal | None = None,
    matched_by: str | None = None,
    notes: str | None = None,
    commit: bool = True,
) -> BslMatch:
    """Allocate ``amount`` of ``bsl_id`` to ``target_type:target_id``.

    Validation:
        * target_type must be PAYMENT or JOURNAL_ENTRY
        * BSL must exist and be non-archived
        * target row must exist and belong to the BSL's company
          (cross-company leak guard — RLS only catches cross-tenant)
        * amount sign must match BSL.amount sign
        * sum(existing allocations) + amount must not exceed |BSL.amount|
        * journal-entry targets must be POSTED

    Side effects:
        * Inserts a ``bsl_matches`` row (commit unless commit=False)
        * Recomputes BSL status (UNMATCHED / PARTIAL / MATCHED)
        * Mirrors the latest/largest match into BSL.matched_to_* and
          BSL.matched_entry_id for back-compat
    """
    if target_type not in _VALID_TARGETS:
        raise ValueError(
            f"target_type must be PAYMENT or JOURNAL_ENTRY, got {target_type!r}"
        )

    bsl = await session.get(BankStatementLine, bsl_id)
    if bsl is None or bsl.archived_at is not None:
        raise ValueError("Statement line not found")

    # Cross-company check: target row must live in the same company.
    # RLS only gates cross-tenant; within a tenant a forged target_id
    # owned by company B would otherwise silently mis-post into company
    # A's GL. We don't trust the global FK alone — the FK doesn't carry
    # the company.
    Model = _TARGET_MODEL[target_type]
    target_row = await session.get(Model, target_id)
    if target_row is None or target_row.company_id != bsl.company_id:
        raise ValueError(f"{target_type.lower()} {target_id} not found")

    if target_type == TARGET_JOURNAL_ENTRY:
        if target_row.status != EntryStatus.POSTED:
            raise ValueError("Can only match against posted entries")

    # Default to the full residual so single-match callers don't need
    # to pre-compute it.
    existing = await list_matches(session, bsl_id)
    allocated = sum((m.amount for m in existing), Decimal("0"))
    residual = bsl.amount - allocated

    if amount is None:
        amount = residual
    else:
        amount = Decimal(amount)

    if amount == 0:
        raise ValueError("Match amount must be non-zero")

    # Sign rule: match sign must agree with BSL sign.
    if (bsl.amount > 0 and amount < 0) or (bsl.amount < 0 and amount > 0):
        raise ValueError(
            f"Match sign {amount} disagrees with BSL amount {bsl.amount}"
        )

    # Over-allocation guard: |allocated + amount| <= |bsl.amount| + tolerance.
    new_total_abs = abs(allocated + amount)
    if new_total_abs > abs(bsl.amount) + _MATCH_TOLERANCE:
        raise ValueError(
            f"Allocation {amount} would over-allocate BSL "
            f"(already {allocated} of {bsl.amount})"
        )

    match = BslMatch(
        bsl_id=bsl_id,
        target_type=target_type,
        target_id=target_id,
        amount=amount,
        company_id=bsl.company_id,
        tenant_id=bsl.tenant_id,
        matched_by=matched_by,
        notes=notes,
    )
    session.add(match)
    await session.flush()

    await _recompute_status(session, bsl, existing + [match])

    if commit:
        await session.commit()
        await session.refresh(match)
    return match


async def remove_match(
    session: AsyncSession,
    match_id: uuid.UUID,
    *,
    commit: bool = True,
) -> None:
    """Soft-delete a match (sets archived_at) and recompute BSL status."""
    match = await session.get(BslMatch, match_id)
    if match is None or match.archived_at is not None:
        raise ValueError("Match not found")

    match.archived_at = datetime.now()
    await session.flush()

    bsl = await session.get(BankStatementLine, match.bsl_id)
    if bsl is not None:
        remaining = await list_matches(session, match.bsl_id)
        await _recompute_status(session, bsl, remaining)

    if commit:
        await session.commit()


async def _recompute_status(
    session: AsyncSession,
    bsl: BankStatementLine,
    matches: list[BslMatch],
) -> None:
    """Set BSL.status from the live junction rows.

    Rules:
        * 0 matches → UNMATCHED
        * |sum(matches)| within ``_MATCH_TOLERANCE`` of |BSL.amount| → MATCHED
        * otherwise → PARTIAL
        * IGNORED is a manual state and is never overwritten here.
    """
    if bsl.status == StatementLineStatus.IGNORED:
        return

    live = [m for m in matches if m.archived_at is None]
    total = sum((m.amount for m in live), Decimal("0"))

    if not live:
        bsl.status = StatementLineStatus.UNMATCHED
        bsl.matched_entry_id = None
        bsl.matched_to_type = None
        bsl.matched_to_id = None
        bsl.matched_at = None
        bsl.matched_by = None
        return

    if abs(abs(total) - abs(bsl.amount)) <= _MATCH_TOLERANCE:
        bsl.status = StatementLineStatus.MATCHED
    else:
        bsl.status = StatementLineStatus.PARTIAL

    _sync_legacy_columns(bsl, live)


def _sync_legacy_columns(
    bsl: BankStatementLine, live_matches: list[BslMatch]
) -> None:
    """Mirror the dominant match into the 1:1 columns.

    Picks the largest by absolute amount (ties → newest). External
    consumers reading ``bsl.matched_to_*`` see "the main thing this
    BSL is reconciled against" which is the right answer for the
    single-match case and a reasonable summary for N:1.
    """
    chosen = max(
        live_matches, key=lambda m: (abs(m.amount), m.created_at or datetime.min)
    )
    bsl.matched_to_type = chosen.target_type
    bsl.matched_to_id = chosen.target_id
    bsl.matched_at = chosen.created_at or datetime.now()
    bsl.matched_by = chosen.matched_by
    bsl.matched_entry_id = (
        chosen.target_id if chosen.target_type == TARGET_JOURNAL_ENTRY else None
    )


# ---------------------------------------------------------------- #
# Legacy 1:1 wrappers — write through the junction                  #
# ---------------------------------------------------------------- #


async def match_line(
    session: AsyncSession,
    line_id: uuid.UUID,
    entry_id: uuid.UUID,
) -> BankStatementLine:
    """Match a statement line to a journal entry (full amount).

    Back-compat wrapper that funnels through ``add_match`` so the
    junction stays authoritative.
    """
    bsl = await session.get(BankStatementLine, line_id)
    if bsl is None:
        raise ValueError("Statement line not found")

    await add_match(
        session,
        bsl_id=line_id,
        target_type=TARGET_JOURNAL_ENTRY,
        target_id=entry_id,
        amount=bsl.amount,
        matched_by="admin",
        commit=False,
    )

    await session.commit()
    await session.refresh(bsl)
    return bsl


async def unmatch_line(
    session: AsyncSession,
    line_id: uuid.UUID,
) -> BankStatementLine:
    """Remove ALL matches from a statement line and reset to UNMATCHED."""
    bsl = await session.get(BankStatementLine, line_id)
    if bsl is None:
        raise ValueError("Statement line not found")

    live = await list_matches(session, line_id)
    now = datetime.now()
    for m in live:
        m.archived_at = now
    await session.flush()

    await _recompute_status(session, bsl, [])

    await session.commit()
    await session.refresh(bsl)
    return bsl


async def split_match_line(
    session: AsyncSession,
    line_id: uuid.UUID,
    *,
    company_id: uuid.UUID,
    allocations: list[dict[str, object]],
    entry_date: date | None = None,
    description: str | None = None,
    posted_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> BankStatementLine:
    """Match a BSL via a new split journal entry."""
    from saebooks.services import journal as journal_svc

    stmt_line = await session.get(BankStatementLine, line_id)
    if stmt_line is None:
        raise ValueError("Statement line not found")
    if stmt_line.archived_at is not None:
        raise ValueError("Statement line not found")
    if stmt_line.company_id != company_id:
        raise ValueError("Statement line not found")
    if stmt_line.status == StatementLineStatus.MATCHED:
        raise ValueError("Statement line is already matched")

    alloc_net_credit = sum(
        Decimal(str(a.get("credit", 0))) - Decimal(str(a.get("debit", 0)))
        for a in allocations
    )
    if alloc_net_credit != stmt_line.amount:
        raise ValueError(
            f"Allocations net credit {alloc_net_credit} does not equal "
            f"bank line amount {stmt_line.amount}. "
            "Allocation credits minus debits must equal the bank line amount."
        )

    if stmt_line.amount >= 0:
        bank_line: dict[str, object] = {
            "account_id": stmt_line.account_id,
            "debit": stmt_line.amount,
            "credit": Decimal("0"),
            "description": description or "Bank deposit",
        }
    else:
        bank_line = {
            "account_id": stmt_line.account_id,
            "debit": Decimal("0"),
            "credit": abs(stmt_line.amount),
            "description": description or "Bank withdrawal",
        }

    journal_lines = [bank_line] + list(allocations)

    txn_date = entry_date or stmt_line.txn_date
    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        entry_date=txn_date,
        description=description or stmt_line.description,
        lines=journal_lines,
        tenant_id=tenant_id,
    )

    await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
    )

    await add_match(
        session,
        bsl_id=line_id,
        target_type=TARGET_JOURNAL_ENTRY,
        target_id=entry.id,
        amount=stmt_line.amount,
        matched_by=posted_by or "api",
        commit=False,
    )

    stmt_line.version += 1

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
