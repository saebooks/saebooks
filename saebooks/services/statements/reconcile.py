"""reconcile.py — pure matching logic for supplier statement reconciliation.

Ported from the statement-recon-prototype's match.py. No DB access —
takes ORM rows as inputs, mutates line.match_status + line.note in place,
and returns a ReconSummary.

AP-as-at formula (accounting-critical):
    our_ap_as_at = sum(bill.total - bill.amount_paid)
    for bills where status in (POSTED, DRAFT) and
    (issue_date is None OR issue_date <= statement_date)

DRAFT bills are included because they are recognised liabilities in our
books (an entered, un-posted bill) and they ARE candidates for matching
(see ``active_bills`` — only VOIDED is excluded). Excluding DRAFT from the
AP population while including it in the matched/NOT_ON_STATEMENT population
makes the two sets disagree, so a matched DRAFT bill would inject its full
total into ``balance_delta`` as a phantom gap (#28 defect 2). Both
populations must be the SAME set.

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
from saebooks.money import money_quantum

_CENT = money_quantum(2)


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
    """Normalise a reference string for loose matching.

    Returns None for anything that is not a non-empty string (None, blank,
    or — defensively — a non-str value), so callers never build lookup keys
    out of placeholder/garbage values.
    """
    if not isinstance(ref, str):
        return None
    return ref.strip().upper().replace(" ", "").replace("-", "") or None


def _draft_suffix(bill: Bill) -> str:
    """Return a ' (bill is DRAFT — not yet posted)' note suffix for DRAFT bills.

    A matched DRAFT bill is a real candidate but its liability is unposted;
    the note qualifies it so an operator sees the provisional state. Empty
    string for non-DRAFT bills.
    """
    if bill.status == BillStatus.DRAFT:
        return " (bill is DRAFT — not yet posted)"
    return ""


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
    # 2. Pre-pass: per-line-type sums + invoice count per reference across
    #    all statement lines. Used to detect SETTLED_NOT_IN_BOOKS without
    #    the false-positive of a single invoice whose reference happens to
    #    net to ~0 via an UNRELATED line (#28 defect 3).
    #
    #    A reference is only "settled" when, FOR THAT SAME REF, the lines
    #    contain BOTH a positive INVOICE and an offsetting negative
    #    PAYMENT/CREDIT of matching magnitude — computed from per-type sums,
    #    not a single blended net. We also refuse to treat a ref as settled
    #    when it is shared across more than one distinct invoice line (the
    #    pairing is ambiguous) or when it is the statement-level
    #    customer_ref / account number (a header value, not a per-line ref).
    # ------------------------------------------------------------------
    ref_invoice_sum: dict[str, Decimal] = {}
    ref_settle_sum: dict[str, Decimal] = {}   # payment + credit (negative)
    ref_invoice_count: dict[str, int] = {}
    for line in lines:
        nref = _norm(line.reference)
        if nref is None:
            continue
        if line.line_type == StatementLineType.INVOICE.value:
            ref_invoice_sum[nref] = ref_invoice_sum.get(nref, Decimal("0")) + line.amount
            ref_invoice_count[nref] = ref_invoice_count.get(nref, 0) + 1
        elif line.line_type in (
            StatementLineType.PAYMENT.value,
            StatementLineType.CREDIT.value,
        ):
            ref_settle_sum[nref] = ref_settle_sum.get(nref, Decimal("0")) + line.amount

    # Statement-level reserved references that must never be treated as a
    # per-invoice ref for settle detection (a customer account number shared
    # across the whole statement is not a closed-out invoice).
    reserved_refs: set[str] = set()
    for reserved in (statement.customer_ref, statement.supplier_abn):
        nreserved = _norm(reserved)
        if nreserved is not None:
            reserved_refs.add(nreserved)

    def _is_closed(nref: str | None) -> bool:
        """True only when this ref has a genuine invoice+offset pair.

        Requires a positive invoice sum and a negative payment/credit sum of
        matching magnitude FOR THE SAME REF, exactly one invoice line on that
        ref, and the ref is not a statement-level reserved value. When in
        doubt this returns False so the caller falls through to
        MISSING_IN_BOOKS rather than silently suppressing a real exception.
        """
        if nref is None or nref in reserved_refs:
            return False
        inv_sum = ref_invoice_sum.get(nref)
        settle_sum = ref_settle_sum.get(nref)
        # Need a positive invoice AND an offsetting payment/credit on this ref.
        if inv_sum is None or inv_sum <= _CENT:
            return False
        if settle_sum is None or settle_sum >= -_CENT:
            return False
        # Ambiguous when more than one distinct invoice shares the ref.
        if ref_invoice_count.get(nref, 0) != 1:
            return False
        # The invoice and its offset must net to ~0 (matching magnitude).
        return abs(inv_sum + settle_sum) <= _CENT

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
                    line.note = _draft_suffix(bill).lstrip()
                else:
                    line.match_status = StatementMatchStatus.AMOUNT_MISMATCH.value
                    line.note = (
                        f"statement {line.amount} vs books {bill.total}"
                        + _draft_suffix(bill)
                    )

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
                        + _draft_suffix(fallback_match)
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
                note=(
                    "in our books, not on the statement — timing or query"
                    + _draft_suffix(bill)
                ),
            )
            not_on_statement.append(synthetic)

    # ------------------------------------------------------------------
    # 5. AP-as-at (accounting-critical formula — see module docstring)
    #
    # Include POSTED *and* DRAFT bills: both are recognised liabilities in
    # our books and both are matched above (only VOIDED is excluded). The AP
    # population must equal the matched/NOT_ON_STATEMENT population, else a
    # matched DRAFT bill leaks its full total into balance_delta (#28 d2).
    # ------------------------------------------------------------------
    _AP_STATUSES = (BillStatus.POSTED, BillStatus.DRAFT)
    our_ap_as_at = Decimal("0")
    for bill in active_bills:
        if bill.status not in _AP_STATUSES:
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
