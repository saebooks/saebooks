"""reconcile.py — pure matching logic for supplier statement reconciliation.

Ported from the statement-recon-prototype's match.py. No DB access —
takes ORM rows as inputs, mutates line.match_status + line.note in place,
and returns a ReconSummary.

AP-as-at formula (accounting-critical):
    our_ap_as_at = sum(bill.total - bill.amount_paid)
    for bills where status == POSTED and (issue_date is None OR issue_date <= statement_date)

balance_delta = (closing_balance or 0) - our_ap_as_at

Design note: ``reconcile_lines`` accepts the statement lines as an explicit
list (not via ``statement.lines``) to avoid triggering SQLAlchemy lazy loads
on the relationship collection. The caller (ingest.py) passes the freshly
created lines directly. NOT_ON_STATEMENT synthetic lines are returned from
``reconcile_lines`` as a separate list; the caller adds them to the session
and to the statement's lines collection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from saebooks.models.bill import Bill, BillStatus
from saebooks.models.supplier_statement import (
    StatementLineType,
    StatementMatchStatus,
    SupplierStatement,
    SupplierStatementLine,
)

_CENT = Decimal("0.01")


@dataclass
class ReconSummary:
    our_ap_as_at: Decimal
    balance_delta: Decimal
    counts: dict[str, int]          # {StatementMatchStatus.value: count}
    open_exceptions: bool           # True if MISSING_IN_BOOKS or AMOUNT_MISMATCH exist
    not_on_statement_lines: list[SupplierStatementLine] = field(default_factory=list)
    """Synthetic NOT_ON_STATEMENT lines (one per unmatched bill).

    The caller is responsible for adding these to the session and to the
    statement's lines collection.
    """


def _norm(ref: str | None) -> str | None:
    """Normalise a reference string for loose matching."""
    if ref is None:
        return None
    return ref.strip().upper().replace(" ", "").replace("-", "") or None


def reconcile_lines(
    statement: SupplierStatement,
    bills: list[Bill],
    statement_lines: list[SupplierStatementLine] | None = None,
) -> ReconSummary:
    """Match statement lines against bills in our books.

    Mutates each ``SupplierStatementLine.match_status`` and ``.note`` in
    place. Does NOT append to ``statement.lines`` (avoids async lazy-load
    in the ingest path).

    ``statement_lines`` — if provided, used as the working set of lines.
    If None, falls back to ``statement.lines`` (safe when the caller has
    already loaded the collection eagerly, e.g. in unit tests).

    NOT_ON_STATEMENT lines for unmatched bills are returned in
    ``ReconSummary.not_on_statement_lines`` for the caller to persist.

    Returns a :class:`ReconSummary`.
    """
    lines = statement_lines if statement_lines is not None else list(statement.lines)

    # ------------------------------------------------------------------
    # 1. Build lookup structures (ignore VOIDED bills)
    # ------------------------------------------------------------------
    active_bills: list[Bill] = [b for b in bills if b.status != BillStatus.VOIDED]

    bills_by_ref: dict[str, Bill] = {}
    for bill in active_bills:
        nref = _norm(bill.supplier_reference)
        if nref is not None:
            bills_by_ref[nref] = bill

    matched_bill_ids: set = set()
    fallback_pool: list[Bill] = list(active_bills)

    # ------------------------------------------------------------------
    # 2. Pre-pass: net amount per reference across all statement lines
    #    (used to detect SETTLED_NOT_IN_BOOKS).
    # ------------------------------------------------------------------
    ref_net: dict[str, Decimal] = {}
    for line in lines:
        nref = _norm(line.reference)
        if nref is None:
            continue
        ref_net[nref] = ref_net.get(nref, Decimal("0")) + line.amount

    def _is_closed(nref: str | None) -> bool:
        if nref is None:
            return False
        return abs(ref_net.get(nref, Decimal("0"))) <= _CENT

    # ------------------------------------------------------------------
    # 3. Process each statement line
    # ------------------------------------------------------------------
    for line in lines:
        if line.line_type == StatementLineType.INVOICE.value:
            nref = _norm(line.reference)

            # Primary match: normalised reference
            if nref and nref in bills_by_ref:
                bill = bills_by_ref[nref]
                matched_bill_ids.add(bill.id)
                fallback_pool = [b for b in fallback_pool if b.id != bill.id]
                line.matched_bill_id = bill.id

                if abs(bill.total - line.amount) <= _CENT:
                    line.match_status = StatementMatchStatus.MATCHED.value
                    line.note = ""
                else:
                    line.match_status = StatementMatchStatus.AMOUNT_MISMATCH.value
                    line.note = f"statement {line.amount} vs books {bill.total}"

            else:
                # Fallback: amount + date proximity (±7 days)
                fallback_match: Bill | None = None
                for candidate in fallback_pool:
                    if candidate.id in matched_bill_ids:
                        continue
                    if abs(candidate.total - line.amount) > _CENT:
                        continue
                    if candidate.issue_date is not None and line.line_date is not None:
                        date_close = abs((candidate.issue_date - line.line_date).days) <= 7
                    else:
                        date_close = False
                    if date_close:
                        fallback_match = candidate
                        break

                if fallback_match is not None:
                    matched_bill_ids.add(fallback_match.id)
                    fallback_pool = [b for b in fallback_pool if b.id != fallback_match.id]
                    line.matched_bill_id = fallback_match.id
                    line.match_status = StatementMatchStatus.MATCHED.value
                    line.note = (
                        f"matched by amount+date; ref differs "
                        f"(stmt {line.reference} vs books {fallback_match.supplier_reference})"
                    )
                else:
                    nref_line = _norm(line.reference)
                    if _is_closed(nref_line):
                        line.match_status = StatementMatchStatus.SETTLED_NOT_IN_BOOKS.value
                        line.note = (
                            "invoiced & paid within statement period; "
                            "not recorded in books — informational, no balance impact"
                        )
                    else:
                        line.match_status = StatementMatchStatus.MISSING_IN_BOOKS.value
                        line.note = "on statement, not in our books — likely an unrecorded bill"

        elif line.line_type in (
            StatementLineType.PAYMENT.value,
            StatementLineType.CREDIT.value,
        ):
            line.match_status = StatementMatchStatus.PAYMENT_INFO.value
            line.note = f"payment/credit shown on statement: {line.amount}"

        # ADJUSTMENT and UNKNOWN lines: leave as UNMATCHED

    # ------------------------------------------------------------------
    # 4. Unmatched bills → NOT_ON_STATEMENT synthetic line objects
    #    (returned to caller; NOT appended to statement.lines here)
    # ------------------------------------------------------------------
    not_on_statement: list[SupplierStatementLine] = []
    for bill in active_bills:
        if bill.id not in matched_bill_ids:
            synthetic = SupplierStatementLine(
                tenant_id=statement.tenant_id,
                statement_id=statement.id,
                line_date=bill.issue_date,
                line_type=StatementLineType.INVOICE.value,
                reference=bill.supplier_reference,
                description=f"Bill {bill.number or ''} — not on statement".strip(" —"),
                amount=bill.total,
                match_status=StatementMatchStatus.NOT_ON_STATEMENT.value,
                matched_bill_id=bill.id,
                note="in our books, not on the statement — timing or query",
            )
            not_on_statement.append(synthetic)

    # ------------------------------------------------------------------
    # 5. AP-as-at (accounting-critical formula — see module docstring)
    # ------------------------------------------------------------------
    our_ap_as_at = Decimal("0")
    for bill in active_bills:
        if bill.status != BillStatus.POSTED:
            continue
        if statement.statement_date is None or bill.issue_date is None or bill.issue_date <= statement.statement_date:
            our_ap_as_at += bill.total - bill.amount_paid

    # ------------------------------------------------------------------
    # 6. Balance delta
    # ------------------------------------------------------------------
    balance_delta = (statement.closing_balance or Decimal("0")) - our_ap_as_at

    # ------------------------------------------------------------------
    # 7. Summary counts (include synthetic NOT_ON_STATEMENT in counts)
    # ------------------------------------------------------------------
    all_lines = lines + not_on_statement
    counts: dict[str, int] = {}
    for line in all_lines:
        key = line.match_status
        counts[key] = counts.get(key, 0) + 1

    open_exceptions = bool(
        counts.get(StatementMatchStatus.MISSING_IN_BOOKS.value, 0)
        + counts.get(StatementMatchStatus.AMOUNT_MISMATCH.value, 0)
    )

    return ReconSummary(
        our_ap_as_at=our_ap_as_at,
        balance_delta=balance_delta,
        counts=counts,
        open_exceptions=open_exceptions,
        not_on_statement_lines=not_on_statement,
    )
