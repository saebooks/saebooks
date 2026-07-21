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
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.bsl_match import (
    TARGET_JOURNAL_ENTRY,
    TARGET_PAYMENT,
    BslMatch,
)
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.payment import Payment

_VALID_TARGETS = {TARGET_PAYMENT, TARGET_JOURNAL_ENTRY}
_TARGET_MODEL = {TARGET_PAYMENT: Payment, TARGET_JOURNAL_ENTRY: JournalEntry}

# Match provenance (R8b, migration 0220) — how an allocation came to be.
MATCHED_VIA_MANUAL = "MANUAL"
MATCHED_VIA_AUTO = "AUTO"
MATCHED_VIA_RULE = "RULE"
MATCHED_VIA_COMPOUND = "COMPOUND"
_VALID_MATCHED_VIA = {
    MATCHED_VIA_MANUAL,
    MATCHED_VIA_AUTO,
    MATCHED_VIA_RULE,
    MATCHED_VIA_COMPOUND,
}

# Suggest/auto_match scoring (R8a/R8d) — confidence tiers and reasons.
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
REASON_EXACT_AMOUNT = "EXACT_AMOUNT"
REASON_AMOUNT_AND_DATE = "AMOUNT_AND_DATE"
REASON_AMOUNT_AND_REFERENCE = "AMOUNT_AND_REFERENCE"
REASON_RULE_PATTERN = "RULE_PATTERN"

# A bank line clearing a day or two after the source transaction date is
# routine (weekend/processing lag) — treat entries within this window as
# a "date match" for scoring purposes, not just an exact-day match.
_DATE_PROXIMITY_DAYS = 3

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


def _statement_line_conditions(
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    status: StatementLineStatus | None,
) -> list[Any]:
    conditions: list[Any] = [
        BankStatementLine.company_id == company_id,
        BankStatementLine.account_id == account_id,
    ]
    if status:
        conditions.append(BankStatementLine.status == status)
    return conditions


async def statement_lines(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: StatementLineStatus | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[BankStatementLine]:
    """Get statement lines for a bank account, optionally filtered."""
    stmt = (
        select(BankStatementLine)
        .where(and_(*_statement_line_conditions(company_id, account_id, status)))
        .order_by(BankStatementLine.txn_date, BankStatementLine.created_at)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_statement_lines(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: StatementLineStatus | None = None,
) -> int:
    """Count statement lines matching the same filters as statement_lines."""
    stmt = select(func.count()).select_from(BankStatementLine).where(
        and_(*_statement_line_conditions(company_id, account_id, status))
    )
    return (await session.execute(stmt)).scalar_one()


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
# Suggest scoring (R8a) — shared by GET /suggest and auto_match     #
# ---------------------------------------------------------------- #


def _reference_overlap(stmt_line: BankStatementLine, entry: JournalEntry) -> bool:
    """True if the BSL's reference/description shows up in the entry.

    Case-insensitive SUBSTRING containment against the entry's ``ref`` and
    ``description`` — deliberately not word-boundary tokenised. Known
    accepted risk: a short-ish numeric reference (e.g. "1050") can match
    inside a longer unrelated number ("21050") and promote a same-amount
    candidate to HIGH. Bounded because the amount must already match
    exactly and auto_match only links (never posts), but do not lower the
    len >= 4 floor without adding word-boundary tokenisation.
    """
    haystack = " ".join(
        filter(None, [entry.description, entry.ref])
    ).lower()
    if not haystack:
        return False
    for candidate in (stmt_line.reference, stmt_line.description):
        if not candidate:
            continue
        needle = candidate.strip().lower()
        if len(needle) >= 4 and needle in haystack:
            return True
    return False


def _date_proximity(stmt_line: BankStatementLine, entry: JournalEntry) -> bool:
    """True if the entry's date is within ``_DATE_PROXIMITY_DAYS`` of the BSL."""
    return abs((entry.entry_date - stmt_line.txn_date).days) <= _DATE_PROXIMITY_DAYS


async def score_candidate(
    session: AsyncSession,
    company_id: uuid.UUID,
    stmt_line: BankStatementLine,
    entry: JournalEntry,
) -> tuple[str, str, uuid.UUID | None]:
    """Score a candidate entry against a statement line.

    Returns ``(confidence, match_reason, rule_id)``. Candidates reaching
    this function are already exact-amount matches (``candidate_entries``
    filters on that) — scoring only ranks them, it never loosens the
    underlying amount match (R8 risk control).

    Tiers, most to least specific:
        * RULE_PATTERN  — a ``bank_rules`` rule matches the BSL's
          description → HIGH, ``rule_id`` set.
        * AMOUNT_AND_REFERENCE — the BSL's reference/description shows
          up in the entry's ref/description → HIGH.
        * AMOUNT_AND_DATE — entry date within
          ``_DATE_PROXIMITY_DAYS`` of the BSL's txn_date → MEDIUM.
        * EXACT_AMOUNT — no other corroborating signal → LOW.
    """
    from saebooks.services import bank_rules as bank_rules_svc

    rule = await bank_rules_svc.find_matching_rule(
        session, company_id, stmt_line.description or ""
    )
    if rule is not None:
        return CONFIDENCE_HIGH, REASON_RULE_PATTERN, rule.id

    if _reference_overlap(stmt_line, entry):
        return CONFIDENCE_HIGH, REASON_AMOUNT_AND_REFERENCE, None

    if _date_proximity(stmt_line, entry):
        return CONFIDENCE_MEDIUM, REASON_AMOUNT_AND_DATE, None

    return CONFIDENCE_LOW, REASON_EXACT_AMOUNT, None


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
    matched_via: str = MATCHED_VIA_MANUAL,
    rule_id: uuid.UUID | None = None,
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
        * matched_via must be one of MANUAL/AUTO/RULE/COMPOUND (R8b)

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
    if matched_via not in _VALID_MATCHED_VIA:
        raise ValueError(
            f"matched_via must be one of {sorted(_VALID_MATCHED_VIA)}, "
            f"got {matched_via!r}"
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

    if target_type == TARGET_JOURNAL_ENTRY and target_row.status != EntryStatus.POSTED:
        raise ValueError("Can only match against posted entries")

    # Default to the full residual so single-match callers don't need
    # to pre-compute it.
    existing = await list_matches(session, bsl_id)
    allocated = sum((m.amount for m in existing), Decimal("0"))
    residual = bsl.amount - allocated

    amount = residual if amount is None else Decimal(amount)

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
        matched_via=matched_via,
        rule_id=rule_id,
    )
    session.add(match)
    await session.flush()

    await _recompute_status(session, bsl, [*existing, match])

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


async def auto_match(
    session: AsyncSession,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
) -> dict[str, int]:
    """Run "honest" automatic matching for an account (R8d).

    For each UNMATCHED line, score every candidate (see
    ``score_candidate``) and link ONLY when exactly one candidate scores
    HIGH confidence. This never loosens matching (candidates are still
    exact-amount) and never posts anything — it only links already-POSTED
    entries, same invariant as before R8.

    Returns ``{"matched": N, "skipped_ambiguous": M, "skipped_no_candidate": K}``:
        * matched — a single HIGH-confidence candidate was linked.
        * skipped_ambiguous — 2+ candidates tied at HIGH confidence; the
          caller must resolve manually (POST /reconciliation/match).
        * skipped_no_candidate — zero candidates, or candidates existed
          but none reached HIGH confidence.
    """
    matched = 0
    skipped_ambiguous = 0
    skipped_no_candidate = 0

    unmatched = await statement_lines(
        session, company_id, account_id, status=StatementLineStatus.UNMATCHED
    )

    for line in unmatched:
        candidates = await candidate_entries(session, company_id, account_id, line)
        if not candidates:
            skipped_no_candidate += 1
            continue

        high: list[tuple[JournalEntry, str, uuid.UUID | None]] = []
        for entry in candidates:
            confidence, reason, rule_id = await score_candidate(
                session, company_id, line, entry
            )
            if confidence == CONFIDENCE_HIGH:
                high.append((entry, reason, rule_id))

        if not high:
            skipped_no_candidate += 1
            continue
        if len(high) > 1:
            skipped_ambiguous += 1
            continue

        entry, reason, rule_id = high[0]
        try:
            await add_match(
                session,
                bsl_id=line.id,
                target_type=TARGET_JOURNAL_ENTRY,
                target_id=entry.id,
                amount=line.amount,
                matched_by="auto-match",
                matched_via=(
                    MATCHED_VIA_RULE if reason == REASON_RULE_PATTERN
                    else MATCHED_VIA_AUTO
                ),
                rule_id=rule_id,
                commit=True,
            )
            matched += 1
        except ValueError:
            # Entry already matched or line state changed underneath us
            # (e.g. a concurrent manual match) — skip, don't blow up the
            # whole batch.
            skipped_no_candidate += 1

    return {
        "matched": matched,
        "skipped_ambiguous": skipped_ambiguous,
        "skipped_no_candidate": skipped_no_candidate,
    }


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

    journal_lines = [bank_line, *list(allocations)]

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
        origin=JournalOrigin.BANK_REC,
        source_type="bank_statement_line",
        source_id=line_id,
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


# ---------------------------------------------------------------- #
# Compound create-and-match (R8c)                                  #
# ---------------------------------------------------------------- #

_CREATE_AND_MATCH_RECORD_TYPES = {"expense", "payment"}


async def create_and_match(
    session: AsyncSession,
    line_id: uuid.UUID,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    record_type: str,
    expense_spec: dict[str, object] | None = None,
    payment_spec: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create a record (expense or payment), post it, and match it to a
    bank statement line — one call, one bookkeeping action.

    Composes the SAME service-layer primitives the individual
    ``/expenses`` and ``/payments`` endpoints call (``api_create`` +
    ``api_post_expense`` / ``api_post_payment``) — never a manual journal
    entry (golden rule). The record's amount/account/direction are ALWAYS
    derived from the bank line server-side, never accepted from the
    caller, so the created record and the match can never disagree on
    amount or sign.

    Atomicity — read this before assuming "one transaction" end to end:
        * The DRAFT record creation phase (``api_create(commit=False)``)
          is genuinely atomic with its own validation: if the record
          spec's total doesn't reconcile against the bank line amount,
          the session is rolled back and NOTHING persists.
        * The POST phase (``api_post_expense`` / ``api_post_payment``)
          calls into ``journal_svc.create_draft`` / ``journal_svc.post``,
          which commit internally (same as every other posting path in
          this codebase — see ``post_expense``/``post_payment``). A
          failure here leaves a POSTED-or-DRAFT record with no match —
          recoverable (void/delete/re-post it), never a corrupt or
          partial ``bsl_matches`` row.
        * The final ``add_match`` call is its own commit. A failure here
          leaves a valid POSTED record with no match — again recoverable,
          never partial.
        * "No partial matches ever persisted" (the hard constraint) IS
          satisfied: a ``bsl_matches`` row only ever exists fully-formed
          or not at all. What is NOT achieved is "one atomic transaction"
          across the whole create+post+match pipeline — that would
          require no-commit twins of the record-posting engines, which
          is out of scope for this slice (flagged in the R8 report).

    Raises ``ValueError`` (caught by the router → 404/422):
        * BSL not found / wrong company / already MATCHED
        * record_type not in {"expense", "payment"}
        * record spec missing for the given record_type
        * expense: BSL is not a withdrawal (expenses only debit spend)
        * expense: line totals don't reconcile against the BSL amount
    """
    from saebooks.services import expenses as expenses_svc
    from saebooks.services import payments as payments_svc
    from saebooks.services.payments import PaymentDirection, PaymentMethod

    bsl = await session.get(BankStatementLine, line_id)
    if bsl is None or bsl.archived_at is not None:
        raise ValueError("Statement line not found")
    if bsl.company_id != company_id:
        raise ValueError("Statement line not found")
    if bsl.status in (StatementLineStatus.MATCHED, StatementLineStatus.PARTIAL):
        # PARTIAL lines are rejected too: the record below is created for the
        # FULL bsl.amount, which would always over-allocate against a line
        # that already carries partial matches.
        raise ValueError("Statement line is already matched")

    if record_type not in _CREATE_AND_MATCH_RECORD_TYPES:
        raise ValueError(
            f"Unsupported record_type {record_type!r}; expected one of "
            f"{sorted(_CREATE_AND_MATCH_RECORD_TYPES)}"
        )

    if record_type == "expense":
        if expense_spec is None:
            raise ValueError("record_type=expense requires an expense spec")
        if bsl.amount >= 0:
            raise ValueError(
                "An expense can only match a withdrawal (negative bank "
                "line amount) — this line is a deposit"
            )

        expense = await expenses_svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=actor,
            payment_account_id=bsl.account_id,
            expense_date=expense_spec.get("expense_date") or bsl.txn_date,
            contact_id=expense_spec.get("contact_id"),
            lines=expense_spec.get("lines"),
            reference=expense_spec.get("reference"),
            notes=expense_spec.get("notes"),
            currency=expense_spec.get("currency", "AUD"),
            fx_rate=expense_spec.get("fx_rate"),
            commit=False,
        )

        # Pre-flight amount reconciliation BEFORE committing anything —
        # this is the genuinely-atomic rollback case: a mismatch here
        # leaves nothing persisted. Capture the scalar values we need in
        # the error message BEFORE rollback — session.rollback() expires
        # every ORM object in the session (expire_on_rollback=True), and
        # touching an expired attribute afterwards triggers an implicit
        # lazy-load that isn't safe outside the SQLAlchemy async greenlet.
        expense_base_total = expense.base_total
        bsl_amount = bsl.amount
        if abs(expense_base_total - abs(bsl_amount)) > _MATCH_TOLERANCE:
            await session.rollback()
            raise ValueError(
                f"Expense total {expense_base_total} does not reconcile "
                f"with bank line amount {bsl_amount} (tolerance "
                f"{_MATCH_TOLERANCE}) — nothing was created"
            )

        await session.commit()

        posted_expense = await expenses_svc.api_post_expense(
            session, expense.id, actor, expected_version=1, tenant_id=tenant_id,
        )
        record_id = posted_expense.id
        target_id = posted_expense.journal_entry_id

    else:  # record_type == "payment"
        if payment_spec is None:
            raise ValueError("record_type=payment requires a payment spec")

        direction = (
            PaymentDirection.INCOMING if bsl.amount >= 0
            else PaymentDirection.OUTGOING
        )
        try:
            method = PaymentMethod(str(payment_spec.get("method", "eft")).lower())
        except ValueError as exc:
            raise ValueError(
                f"Invalid payment method {payment_spec.get('method')!r}"
            ) from exc

        payment = await payments_svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=actor,
            contact_id=payment_spec["contact_id"],
            bank_account_id=bsl.account_id,
            payment_date=payment_spec.get("payment_date") or bsl.txn_date,
            amount=abs(bsl.amount),
            direction=direction,
            method=method,
            reference=payment_spec.get("reference"),
            notes=payment_spec.get("notes"),
            currency=payment_spec.get("currency", "AUD"),
            fx_rate=payment_spec.get("fx_rate"),
            allocations=payment_spec.get("allocations"),
        )

        posted_payment = await payments_svc.api_post_payment(
            session, payment.id, actor, expected_version=1, tenant_id=tenant_id,
        )
        record_id = posted_payment.id
        target_id = posted_payment.journal_entry_id

    match = await add_match(
        session,
        bsl_id=line_id,
        target_type=TARGET_JOURNAL_ENTRY,
        target_id=target_id,
        amount=bsl.amount,
        matched_by=actor,
        matched_via=MATCHED_VIA_COMPOUND,
        commit=True,
    )

    refreshed = await session.get(BankStatementLine, line_id)
    return {
        "bsl": refreshed,
        "record_type": record_type,
        "record_id": record_id,
        "journal_entry_id": target_id,
        "match_id": match.id,
    }


def _parse_date(raw: str) -> date | None:
    """Parse date string in YYYY-MM-DD or DD/MM/YYYY format."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
