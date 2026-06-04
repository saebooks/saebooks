"""Tests for services/statements/reconcile.py.

Pure unit tests — no DB required. Build Bill and SupplierStatement fixtures
in-memory and assert match statuses + AP calc.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from saebooks.models.bill import BillStatus
from saebooks.models.supplier_statement import (
    StatementLineType,
    StatementMatchStatus,
    SupplierStatement,
    SupplierStatementLine,
)
from saebooks.services.statements.reconcile import reconcile_lines

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_COMPANY = uuid.UUID("00000000-0000-0000-0000-000000000002")
_STMT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bill(
    *,
    total: str,
    amount_paid: str = "0.00",
    supplier_reference: str | None = None,
    issue_date: date | None = date(2026, 5, 10),
    status: BillStatus = BillStatus.POSTED,
) -> MagicMock:
    b = MagicMock()
    b.id = uuid.uuid4()
    b.total = Decimal(total)
    b.amount_paid = Decimal(amount_paid)
    b.supplier_reference = supplier_reference
    b.issue_date = issue_date
    b.status = status
    b.number = "BILL-001"
    b.contact_id = uuid.uuid4()
    return b


def _make_stmt(
    *,
    statement_date: date = date(2026, 5, 31),
    closing_balance: str = "1100.00",
) -> MagicMock:
    stmt = MagicMock(spec=SupplierStatement)
    stmt.id = _STMT_ID
    stmt.tenant_id = _TENANT
    stmt.company_id = _COMPANY
    stmt.statement_date = statement_date
    stmt.closing_balance = Decimal(closing_balance)
    # Use a real list so tests can inspect and pass it to reconcile_lines
    stmt._lines: list = []
    stmt.lines = stmt._lines
    return stmt


def _add_line(
    stmt: SupplierStatement,
    *,
    amount: str,
    reference: str | None = None,
    line_type: str = StatementLineType.INVOICE.value,
    line_date: date | None = date(2026, 5, 10),
) -> SupplierStatementLine:
    line = MagicMock(spec=SupplierStatementLine)
    line.id = uuid.uuid4()
    line.tenant_id = _TENANT
    line.statement_id = _STMT_ID
    line.amount = Decimal(amount)
    line.reference = reference
    line.line_type = line_type
    line.line_date = line_date
    line.match_status = StatementMatchStatus.UNMATCHED.value
    line.matched_bill_id = None
    line.note = ""
    stmt.lines.append(line)
    return line


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_matched_by_reference():
    """Invoice line with matching reference → MATCHED."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference="INV-1001")
    bill = _make_bill(total="1100.00", supplier_reference="INV-1001")

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.MATCHED.value
    assert line.matched_bill_id == bill.id
    assert summary.counts.get(StatementMatchStatus.MATCHED.value, 0) == 1


def test_amount_mismatch():
    """Same reference, different amount → AMOUNT_MISMATCH."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference="INV-1001")
    bill = _make_bill(total="990.00", supplier_reference="INV-1001")

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.AMOUNT_MISMATCH.value
    assert "990.00" in line.note
    assert summary.open_exceptions is True


def test_missing_in_books():
    """Invoice line with no matching bill → MISSING_IN_BOOKS."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference="INV-9999")

    summary = reconcile_lines(stmt, [], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.MISSING_IN_BOOKS.value
    assert summary.open_exceptions is True


def test_not_on_statement():
    """A bill with no matching statement line → NOT_ON_STATEMENT returned in summary."""
    stmt = _make_stmt(closing_balance="0.00")
    bill = _make_bill(total="500.00", supplier_reference="INV-ORPHAN")

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert len(summary.not_on_statement_lines) == 1
    assert summary.not_on_statement_lines[0].matched_bill_id == bill.id
    assert summary.counts.get(StatementMatchStatus.NOT_ON_STATEMENT.value, 0) == 1


def test_payment_info():
    """Payment line → PAYMENT_INFO."""
    stmt = _make_stmt(closing_balance="0.00")
    line = _add_line(stmt, amount="-500.00", line_type=StatementLineType.PAYMENT.value)

    reconcile_lines(stmt, [], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.PAYMENT_INFO.value


def test_settled_not_in_books():
    """Invoice + payment for same ref, no bill → SETTLED_NOT_IN_BOOKS."""
    stmt = _make_stmt(closing_balance="0.00")
    inv_line = _add_line(stmt, amount="1100.00", reference="INV-SETTLED")
    pay_line = _add_line(stmt, amount="-1100.00", reference="INV-SETTLED", line_type=StatementLineType.PAYMENT.value)

    reconcile_lines(stmt, [])

    assert inv_line.match_status == StatementMatchStatus.SETTLED_NOT_IN_BOOKS.value
    # Payment line → PAYMENT_INFO
    assert pay_line.match_status == StatementMatchStatus.PAYMENT_INFO.value


def test_matched_by_amount_and_date_fallback():
    """Invoice matched by amount+date when reference is different."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference=None, line_date=date(2026, 5, 10))
    bill = _make_bill(total="1100.00", supplier_reference=None, issue_date=date(2026, 5, 12))

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.MATCHED.value
    assert summary.counts.get(StatementMatchStatus.MATCHED.value, 0) == 1


def test_fallback_not_matched_when_date_too_far():
    """Fallback does NOT match when dates are >7 days apart."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference=None, line_date=date(2026, 5, 1))
    bill = _make_bill(total="1100.00", supplier_reference=None, issue_date=date(2026, 5, 15))

    reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.MISSING_IN_BOOKS.value


def test_our_ap_as_at_only_posted_bills():
    """AP-as-at sums only POSTED bills, excluding DRAFT and VOIDED."""
    stmt = _make_stmt(closing_balance="1100.00")

    posted_bill = _make_bill(total="1100.00", amount_paid="0.00", status=BillStatus.POSTED, supplier_reference="INV-P")
    draft_bill = _make_bill(total="500.00", amount_paid="0.00", status=BillStatus.DRAFT, supplier_reference="INV-D")
    voided_bill = _make_bill(total="300.00", amount_paid="0.00", status=BillStatus.VOIDED, supplier_reference="INV-V")

    _add_line(stmt, amount="1100.00", reference="INV-P")

    summary = reconcile_lines(stmt, [posted_bill, draft_bill, voided_bill], statement_lines=stmt.lines)

    # Only posted_bill counts: 1100 - 0 = 1100
    assert summary.our_ap_as_at == Decimal("1100.00")


def test_our_ap_as_at_deducts_amount_paid():
    """AP-as-at uses total - amount_paid (not just total)."""
    stmt = _make_stmt(closing_balance="600.00")
    bill = _make_bill(total="1100.00", amount_paid="500.00", status=BillStatus.POSTED, supplier_reference="INV-1")
    _add_line(stmt, amount="1100.00", reference="INV-1")

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert summary.our_ap_as_at == Decimal("600.00")


def test_our_ap_as_at_excludes_future_bills():
    """Bills with issue_date > statement_date are excluded from AP-as-at."""
    stmt = _make_stmt(statement_date=date(2026, 5, 31), closing_balance="0.00")
    future_bill = _make_bill(
        total="1100.00",
        amount_paid="0.00",
        status=BillStatus.POSTED,
        supplier_reference="INV-FUTURE",
        issue_date=date(2026, 6, 15),
    )

    summary = reconcile_lines(stmt, [future_bill], statement_lines=stmt.lines)

    # Future bill is not included in AP-as-at (issue_date > statement_date)
    assert summary.our_ap_as_at == Decimal("0.00")
    # It appears as NOT_ON_STATEMENT synthetic
    assert len(summary.not_on_statement_lines) == 1


def test_balance_delta():
    """balance_delta = closing_balance - our_ap_as_at."""
    stmt = _make_stmt(closing_balance="1200.00")
    bill = _make_bill(total="1100.00", amount_paid="0.00", status=BillStatus.POSTED, supplier_reference="INV-X")
    _add_line(stmt, amount="1100.00", reference="INV-X")

    summary = reconcile_lines(stmt, [bill], statement_lines=stmt.lines)

    assert summary.our_ap_as_at == Decimal("1100.00")
    assert summary.balance_delta == Decimal("100.00")


def test_voided_bills_excluded_from_matching():
    """Voided bills are not candidates for matching."""
    stmt = _make_stmt(closing_balance="1100.00")
    line = _add_line(stmt, amount="1100.00", reference="INV-V")
    voided = _make_bill(total="1100.00", supplier_reference="INV-V", status=BillStatus.VOIDED)

    reconcile_lines(stmt, [voided], statement_lines=stmt.lines)

    assert line.match_status == StatementMatchStatus.MISSING_IN_BOOKS.value


def test_no_bills_all_missing():
    """All invoice lines are MISSING_IN_BOOKS when no bills at all."""
    stmt = _make_stmt(closing_balance="2200.00")
    l1 = _add_line(stmt, amount="1100.00", reference="INV-A")
    l2 = _add_line(stmt, amount="1100.00", reference="INV-B")

    summary = reconcile_lines(stmt, [], statement_lines=stmt.lines)

    assert l1.match_status == StatementMatchStatus.MISSING_IN_BOOKS.value
    assert l2.match_status == StatementMatchStatus.MISSING_IN_BOOKS.value
    assert summary.open_exceptions is True
    assert summary.our_ap_as_at == Decimal("0.00")
