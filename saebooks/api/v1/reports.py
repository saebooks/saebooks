"""JSON router — ``/api/v1/reports``.

Tier-5 report endpoints: Aged Receivables, Aged Payables, Profit & Loss,
and Balance Sheet.

Aged reports
------------
Both AR/AP reports walk the ``invoices`` / ``bills`` tables directly (not
the GL) so they can show per-document outstanding balances.  The outstanding
balance for each document is ``total - amount_paid`` (both fields exist
on the model).

Open-document filter
--------------------
* AR: ``InvoiceStatus.POSTED`` (``DRAFT`` is uncommitted; ``VOIDED``
  reverses the receivable).  The model enum has three values only:
  DRAFT / POSTED / VOIDED — there are no SENT, PARTIALLY_PAID, or
  OVERDUE variants in this codebase; POSTED covers all in-flight AR.
* AP: same logic for ``BillStatus.POSTED``.

Bucket-day thresholds
---------------------
The ``bucket_days`` query parameter (default ``[0, 30, 60, 90]``)
controls the day-count boundaries.  With the default you get:

    current   — due_date >= as_of_date  (days_overdue <= 0)
    1-30 days — days_overdue in [1..30]
    31-60 days
    61-90 days
    90+ days

A custom value of e.g. ``[0, 14, 60]`` produces:
    current / 1-14 days / 15-60 days / 60+ days

GL reports (P&L + Balance Sheet)
----------------------------------
Both financial statements are derived from the ``journal_lines`` table
joined to ``journal_entries`` (filtered by status) and ``accounts``.

* P&L: sums JournalLine debit/credit over a date range.  Income accounts:
  net = credit - debit (credits increase income).  Expense accounts:
  net = debit - credit.  Groups by AccountType.
* Balance Sheet: cumulative sum of all JournalLine entries up to
  ``as_of_date``.  Asset accounts: balance = debit - credit.  Liability
  and equity accounts: balance = credit - debit.  Checks whether
  total_assets == total_liabilities + total_equity (balanced flag).

Tenant isolation
----------------
Queries are scoped to the tenant resolved from
``SAEBOOKS_DEV_TENANT_ID`` (or the default tenant) and to the first
active company (single-company phase-1 assumption).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    AgedReport,
    BASSummary,
    BSReport,
    BudgetVsActualLine,
    BudgetVsActualReport,
    CashflowStatement,
    DepreciationAssetLine,
    DepreciationSchedule,
    FXRevaluationItem,
    FXRevaluationReport,
    PLBySegmentReport,
    PLSegmentAccountLine,
    PLSegmentRow,
    PLSegmentSection,
    PnLReport,
    RevenueByCustomerReport,
    RevenueByCustomerRow,
    TrialBalanceLine,
    TrialBalanceReport,
    YTDTurnoverReport,
)
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.budget import Budget
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.depreciation_model import DepreciationModel
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import assets as assets_svc
from saebooks.services import reports as reports_svc

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_bearer)],
)

_GST_THRESHOLD = Decimal("75000.00")


def _current_fy_bounds(today: date | None = None) -> tuple[date, date]:
    """Return (fy_start, fy_end) for the Australian FY that contains today.

    Australian FY runs 1 July - 30 June.
    """
    d = today or date.today()
    if d.month >= 7:
        fy_start = date(d.year, 7, 1)
        fy_end = date(d.year + 1, 6, 30)
    else:
        fy_start = date(d.year - 1, 7, 1)
        fy_end = date(d.year, 6, 30)
    return fy_start, fy_end


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_bucket_labels(bucket_days: list[int]) -> list[str]:
    """Derive ordered bucket label strings from the sorted day thresholds.

    ``bucket_days`` must be a sorted list of non-negative integers that
    includes 0 as the first element (current boundary).  Example:
    ``[0, 30, 60, 90]`` → ``["current", "1-30 days", "31-60 days",
    "61-90 days", "90+ days"]``.
    """
    labels: list[str] = ["current"]
    for i, lo in enumerate(bucket_days[1:], start=1):
        prev = bucket_days[i - 1]
        hi = lo
        labels.append(f"{prev + 1}-{hi} days")
    # The final open-ended bucket
    last = bucket_days[-1]
    labels.append(f"{last}+ days")
    return labels


def _days_to_bucket(days_overdue: int, bucket_days: list[int]) -> str:
    """Map a days-overdue integer to a label string.

    ``days_overdue <= 0`` is always "current".  Then we walk the
    thresholds in ascending order — the first threshold that is >=
    ``days_overdue`` gives the label index.
    """
    if days_overdue <= 0:
        return "current"
    for i, threshold in enumerate(bucket_days[1:], start=1):
        if days_overdue <= threshold:
            prev = bucket_days[i - 1]
            return f"{prev + 1}-{threshold} days"
    last = bucket_days[-1]
    return f"{last}+ days"


def _validate_bucket_days(raw: list[int]) -> list[int]:
    """Validate and return a clean sorted bucket_days list.

    Raises HTTPException(422) if the list is invalid.
    """
    if not raw:
        raise HTTPException(422, "bucket_days must not be empty")
    if any(d < 0 for d in raw):
        raise HTTPException(422, "bucket_days values must be >= 0")
    cleaned = sorted(set(raw))
    if cleaned[0] != 0:
        raise HTTPException(422, "bucket_days must include 0 as the first boundary")
    return cleaned


def _build_report(
    rows: list[tuple[Any, str]],  # (Invoice|Bill, contact_name)
    as_of: date,
    bucket_days: list[int],
    bucket_labels: list[str],
    retention_amounts: dict[UUID, Decimal] | None = None,
) -> AgedReport:
    """Assemble an AgedReport from DB rows.

    When ``retention_amounts`` is supplied (AR only), each invoice's
    outstanding balance is split: the retention portion (up to the invoice's
    retention_amount) lands in ``retentions_receivable`` buckets, and only
    the trade-debtor remainder appears in the per-contact rows and ``totals``.
    Payments are assumed to reduce the trade-debtor portion first, so
    retentions are the last to be cleared.
    """
    zero = Decimal("0")

    # contact_id → {"contact_id": ..., "contact_name": ..., <bucket>: ...}
    groups: dict[UUID, dict[str, Any]] = {}

    # retentions_receivable totals by bucket label (AR only)
    ret_buckets: dict[str, Decimal] = {lbl: zero for lbl in bucket_labels}
    ret_total = zero

    for doc, contact_name in rows:
        contact_id: UUID = doc.contact_id
        outstanding: Decimal = doc.total - doc.amount_paid
        days_overdue: int = (as_of - doc.due_date).days
        label = _days_to_bucket(days_overdue, bucket_days)

        # Split outstanding into trade-debtor and retention portions.
        if retention_amounts is not None:
            ret_amt = retention_amounts.get(doc.id, zero)
            # Payments reduce Trade Debtors first; retention is last to clear.
            ret_outstanding = min(ret_amt, outstanding)
            trade_balance = outstanding - ret_outstanding
        else:
            ret_outstanding = zero
            trade_balance = outstanding

        # Accumulate retention bucket totals
        if ret_outstanding > zero:
            ret_buckets[label] = ret_buckets[label] + ret_outstanding
            ret_total += ret_outstanding

        if contact_id not in groups:
            groups[contact_id] = {
                "contact_id": str(contact_id),
                "contact_name": contact_name,
                **{lbl: zero for lbl in bucket_labels},
                "total": zero,
            }

        groups[contact_id][label] = groups[contact_id][label] + trade_balance
        groups[contact_id]["total"] = groups[contact_id]["total"] + trade_balance

    # Filter out contacts whose trade-debtor balance is zero (full retention)
    sorted_groups = sorted(
        [g for g in groups.values() if g["total"] > zero],
        key=lambda g: g["total"],
        reverse=True,
    )

    # Grand totals (trade debtors only)
    totals: dict[str, Any] = {lbl: zero for lbl in bucket_labels}
    totals["total"] = zero
    for g in sorted_groups:
        for lbl in bucket_labels:
            totals[lbl] = totals[lbl] + g[lbl]
        totals["total"] = totals["total"] + g["total"]

    # Convert Decimal to float for JSON serialisation consistency
    def _floatify(d: dict[str, Any]) -> dict[str, Any]:
        return {
            k: float(v) if isinstance(v, Decimal) else v
            for k, v in d.items()
        }

    # Build retentions_receivable summary (only if AR report with retentions)
    retentions_receivable: dict | None = None
    if retention_amounts is not None and ret_total > zero:
        rr: dict[str, Any] = dict(ret_buckets)
        rr["total"] = ret_total
        retentions_receivable = _floatify(rr)

    return AgedReport(
        as_of_date=as_of,
        buckets=bucket_labels,
        contacts=[_floatify(g) for g in sorted_groups],
        totals=_floatify(totals),
        retentions_receivable=retentions_receivable,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/aged_receivables
# ---------------------------------------------------------------------------


@router.get("/aged_receivables", response_model=AgedReport)
async def aged_receivables(
    request: Request,
    as_of_date: date | None = Query(default=None),
    bucket_days: list[int] = Query(default=[0, 30, 60, 90]),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AgedReport:
    """Aged receivables as at ``as_of_date`` (default today).

    Returns open POSTED invoices grouped by contact, bucketed by
    days overdue.  Outstanding balance = ``total - amount_paid``.

    Open AR status: ``InvoiceStatus.POSTED`` — the only status that has
    a GL impact and an unpaid balance.  DRAFT invoices are uncommitted;
    VOIDED invoices have no balance.
    """
    as_of = as_of_date or date.today()
    bd = _validate_bucket_days(bucket_days)
    labels = _build_bucket_labels(bd)

    tenant_id = resolve_tenant_id(request)

    stmt = (
        select(Invoice, Contact.name)
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(
            and_(
                Invoice.company_id == company_id,
                Invoice.tenant_id == tenant_id,
                Invoice.status == InvoiceStatus.POSTED,
                Invoice.archived_at.is_(None),
                Invoice.total > Invoice.amount_paid,
                Invoice.issue_date <= as_of,
            )
        )
        .order_by(Contact.name, Invoice.due_date)
    )
    rows = (await session.execute(stmt)).all()

    # Build per-invoice retention amounts from invoice_lines so the report
    # can display Retentions Receivable as a separate line from Trade Debtors.
    invoice_ids = [doc.id for doc, _ in rows]
    retention_amounts: dict[UUID, Decimal] = {}
    if invoice_ids:
        ret_stmt = (
            select(
                InvoiceLine.invoice_id,
                func.sum(
                    InvoiceLine.line_subtotal * InvoiceLine.retention_pct / Decimal("100")
                ).label("retention_amount"),
            )
            .where(
                InvoiceLine.invoice_id.in_(invoice_ids),
                InvoiceLine.retention_pct > Decimal("0"),
            )
            .group_by(InvoiceLine.invoice_id)
        )
        for inv_id, amt in (await session.execute(ret_stmt)).all():
            if amt and amt > Decimal("0"):
                retention_amounts[inv_id] = amt

    return _build_report(rows, as_of, bd, labels, retention_amounts=retention_amounts)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/aged_payables
# ---------------------------------------------------------------------------


@router.get("/aged_payables", response_model=AgedReport)
async def aged_payables(
    request: Request,
    as_of_date: date | None = Query(default=None),
    bucket_days: list[int] = Query(default=[0, 30, 60, 90]),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AgedReport:
    """Aged payables as at ``as_of_date`` (default today).

    Returns open POSTED bills grouped by contact (supplier), bucketed by
    days overdue.  Outstanding balance = ``total - amount_paid``.

    Open AP status: ``BillStatus.POSTED`` — the only status that has a
    GL impact and an unpaid balance.
    """
    as_of = as_of_date or date.today()
    bd = _validate_bucket_days(bucket_days)
    labels = _build_bucket_labels(bd)

    tenant_id = resolve_tenant_id(request)

    stmt = (
        select(Bill, Contact.name)
        .join(Contact, Bill.contact_id == Contact.id)
        .where(
            and_(
                Bill.company_id == company_id,
                Bill.tenant_id == tenant_id,
                Bill.status == BillStatus.POSTED,
                Bill.archived_at.is_(None),
                Bill.total > Bill.amount_paid,
                Bill.issue_date <= as_of,
            )
        )
        .order_by(Contact.name, Bill.due_date)
        )
    rows = (await session.execute(stmt)).all()

    return _build_report(rows, as_of, bd, labels)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/profit_loss
# ---------------------------------------------------------------------------

# AccountTypes that are income accounts (credits increase balance).
_INCOME_TYPES = {AccountType.INCOME, AccountType.OTHER_INCOME}

# AccountTypes that are expense accounts (debits increase balance).
_EXPENSE_TYPES = {AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE}


@router.get("/profit_loss", response_model=PnLReport)
async def profit_loss(
    request: Request,
    from_date: date = Query(...),
    to_date: date = Query(...),
    include_draft: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PnLReport:
    """Profit & Loss for a date range.

    Sums JournalLine debit/credit per account for POSTED JEs whose
    ``entry_date`` falls within [from_date, to_date] (inclusive).
    When ``include_draft=True``, DRAFT entries are also included.

    Income accounts: net = credit - debit (credits increase income).
    Expense accounts: net = debit - credit (debits increase expense).
    Only accounts with a non-zero net amount appear in the result.
    """
    statuses = [EntryStatus.POSTED]
    if include_draft:
        statuses.append(EntryStatus.DRAFT)

    tenant_id = resolve_tenant_id(request)

    stmt = (
        select(
            Account.id,
            Account.name,
            Account.code,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status.in_(statuses),
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= from_date,
                JournalEntry.entry_date <= to_date,
            )
        )
        .group_by(Account.id, Account.name, Account.code, Account.account_type)
        .order_by(Account.code)
    )
    rows = (await session.execute(stmt)).all()

    # Accumulate per account-type
    income_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    expenses_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        acc_id, acc_name, acc_code, acc_type, total_debit, total_credit = row
        total_debit = Decimal(total_debit or "0")
        total_credit = Decimal(total_credit or "0")

        if acc_type in _INCOME_TYPES:
            net = float(total_credit - total_debit)
            if net != 0.0:
                income_by_type[acc_type.value].append(
                    {
                        "account_id": acc_id,
                        "account_name": acc_name,
                        "code": acc_code,
                        "amount": net,
                    }
                )
        elif acc_type in _EXPENSE_TYPES:
            net = float(total_debit - total_credit)
            if net != 0.0:
                expenses_by_type[acc_type.value].append(
                    {
                        "account_id": acc_id,
                        "account_name": acc_name,
                        "code": acc_code,
                        "amount": net,
                    }
                )

    total_income = sum(
        line["amount"]
        for lines in income_by_type.values()
        for line in lines
    )
    total_expenses = sum(
        line["amount"]
        for lines in expenses_by_type.values()
        for line in lines
    )

    return PnLReport(
        from_date=from_date,
        to_date=to_date,
        income={
            "INCOME": income_by_type.get("INCOME", []),
            "OTHER_INCOME": income_by_type.get("OTHER_INCOME", []),
            "total_income": total_income,
        },
        expenses={
            "EXPENSE": expenses_by_type.get("EXPENSE", []),
            "COST_OF_SALES": expenses_by_type.get("COST_OF_SALES", []),
            "OTHER_EXPENSE": expenses_by_type.get("OTHER_EXPENSE", []),
            "total_expenses": total_expenses,
        },
        net_profit=total_income - total_expenses,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/balance_sheet
# ---------------------------------------------------------------------------

# AccountTypes whose balance = debit - credit (normal debit balance).
_ASSET_TYPES = {AccountType.ASSET}

# AccountTypes whose balance = credit - debit (normal credit balance).
_LIABILITY_TYPES = {AccountType.LIABILITY}
_EQUITY_TYPES = {AccountType.EQUITY}


@router.get("/balance_sheet", response_model=BSReport)
async def balance_sheet(
    request: Request,
    as_of_date: date = Query(...),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BSReport:
    """Balance sheet as at ``as_of_date``.

    Sums ALL POSTED JournalLine entries where ``entry_date <= as_of_date``
    (cumulative from inception).

    Asset accounts: balance = debit - credit.
    Liability + equity accounts: balance = credit - debit.
    Accounts with a zero net balance are omitted from the response.

    A synthetic "Current Year Earnings" line is always appended to the
    equity section.  It represents the period net income (income credits
    minus expense debits) for all POSTED entries up to ``as_of_date``.
    This matches Xero/MYOB/QBO behaviour for periods that have not been
    formally closed to a retained-earnings equity account.

    ``balanced`` is True when
    ``abs(total_assets - total_liabilities - total_equity) < 0.01``.
    """
    tenant_id = resolve_tenant_id(request)

    stmt = (
        select(
            Account.id,
            Account.name,
            Account.code,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date <= as_of_date,
            )
        )
        .group_by(Account.id, Account.name, Account.code, Account.account_type)
        .order_by(Account.code)
    )
    rows = (await session.execute(stmt)).all()

    assets: list[dict[str, Any]] = []
    liabilities: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []

    # Accumulators for Current Year Earnings (CYE) synthesis.
    # Income is credit-normal: net_income_credit accumulates (credit - debit).
    # Expense is debit-normal: net_expense_debit accumulates (debit - credit).
    cye_income_credit = Decimal("0")   # credit-normal income contribution
    cye_expense_debit = Decimal("0")   # debit-normal expense contribution

    for row in rows:
        acc_id, acc_name, acc_code, acc_type, total_debit, total_credit = row
        total_debit = Decimal(total_debit or "0")
        total_credit = Decimal(total_credit or "0")

        if acc_type in _ASSET_TYPES:
            bal = float(total_debit - total_credit)
            if bal != 0.0:
                assets.append(
                    {
                        "account_id": acc_id,
                        "account_name": acc_name,
                        "code": acc_code,
                        "balance": bal,
                    }
                )
        elif acc_type in _LIABILITY_TYPES:
            bal = float(total_credit - total_debit)
            if bal != 0.0:
                liabilities.append(
                    {
                        "account_id": acc_id,
                        "account_name": acc_name,
                        "code": acc_code,
                        "balance": bal,
                    }
                )
        elif acc_type in _EQUITY_TYPES:
            bal = float(total_credit - total_debit)
            if bal != 0.0:
                equity.append(
                    {
                        "account_id": acc_id,
                        "account_name": acc_name,
                        "code": acc_code,
                        "balance": bal,
                    }
                )
        elif acc_type in _INCOME_TYPES:
            # Credit-normal: income increases with credits.
            cye_income_credit += total_credit - total_debit
        elif acc_type in _EXPENSE_TYPES:
            # Debit-normal: expenses increase with debits.
            cye_expense_debit += total_debit - total_credit

    # Synthesise Current Year Earnings.
    # A profitable period (income > expenses) adds credit-normal equity,
    # so the CYE balance in equity terms = income_credit - expense_debit.
    cye_balance = float(cye_income_credit - cye_expense_debit)
    equity.append(
        {
            "account_id": "00000000-0000-0000-0000-000000000000",
            "account_name": "Current Year Earnings",
            "code": "CYE",
            "balance": cye_balance,
        }
    )

    total_assets = sum(line["balance"] for line in assets)
    total_liabilities = sum(line["balance"] for line in liabilities)
    total_equity = sum(line["balance"] for line in equity)
    difference = abs(total_assets - total_liabilities - total_equity)

    return BSReport(
        as_of_date=as_of_date,
        assets={"ASSET": assets, "total_assets": total_assets},
        liabilities={"LIABILITY": liabilities, "total_liabilities": total_liabilities},
        equity={"EQUITY": equity, "total_equity": total_equity},
        balanced=difference < 0.01,
        difference=round(difference, 2),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/bas_summary
# ---------------------------------------------------------------------------

# Reporting types that contribute to taxable BAS buckets.
_TAXABLE_REPORTING_TYPE = "taxable"
_GST_FREE_REPORTING_TYPE = "gst_free"
# C3: G2/G10 reporting types — kept in lock-step with the strings
# au.bas_report matches ("export" -> G2, "capital" -> G10).
_EXPORT_REPORTING_TYPE = "export"
_CAPITAL_REPORTING_TYPE = "capital"

# C3: reuse au.bas_report's exact account-type sets for G2/G10 so the
# two BAS implementations sum over identical lines. Imported under
# aliases to keep this module's local _INCOME_TYPES/_EXPENSE_TYPES
# (used by the other report routes) untouched.
from saebooks.services.tax_engine.au import (  # noqa: E402
    _BAS_INCOME_TYPES as _AU_BAS_INCOME_TYPES,
)
from saebooks.services.tax_engine.au import (  # noqa: E402  aliased import, see comment above
    _BAS_PURCHASE_TYPES as _AU_BAS_PURCHASE_TYPES,
)

# GST rate used for 1A calculation (sales).
_GST_RATE = Decimal("0.10")

# GST fraction used for 1B (tax-inclusive purchases): 1/11.
_GST_INCLUSIVE_FRACTION = Decimal("1") / Decimal("11")


async def _bas_aggregate(
    session: AsyncSession,
    company_id: UUID,
    tenant_id: UUID,
    from_date: date,
    to_date: date,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Aggregate G1, G2, G3, G10, G11 totals for POSTED lines in range.

    G2 (export sales) and G10 (capital acquisitions) are computed from
    ``TaxCode.reporting_type`` using the SAME account-type sets and
    formulas as ``services.tax_engine.au.bas_report`` so the two BAS
    implementations agree by construction (C3 reconciliation):

      * G2  = export-tagged INCOME lines, net = credit - debit.
      * G10 = capital-tagged PURCHASE lines (incl. ASSET), net +
              stamped gst_amount = debit - credit + gst_amount.

    G10 sums ``gst_amount`` alongside the net so the result matches
    au.bas_report, which adds the line's stamped GST to the capital
    bucket (G10 is reported GST-inclusive).
    """
    stmt = (
        select(
            Account.account_type,
            TaxCode.reporting_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
            func.sum(func.coalesce(JournalLine.gst_amount, 0)).label("total_gst"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .outerjoin(TaxCode, JournalLine.tax_code_id == TaxCode.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= from_date,
                JournalEntry.entry_date <= to_date,
            )
        )
        .group_by(Account.account_type, TaxCode.reporting_type)
    )
    rows = (await session.execute(stmt)).all()

    g1 = Decimal("0")
    g2 = Decimal("0")
    g3 = Decimal("0")
    g10 = Decimal("0")
    g11 = Decimal("0")

    for acc_type, reporting_type, total_debit, total_credit, total_gst in rows:
        total_debit = Decimal(total_debit or "0")
        total_credit = Decimal(total_credit or "0")
        total_gst = Decimal(total_gst or "0")
        rt = reporting_type or ""

        # Income side. ``_AU_BAS_INCOME_TYPES`` mirrors au.bas_report's
        # income set so G2 lands on exactly the same lines au counts.
        if acc_type in _AU_BAS_INCOME_TYPES:
            net = total_credit - total_debit
            if rt == _TAXABLE_REPORTING_TYPE:
                g1 += net
            elif rt == _EXPORT_REPORTING_TYPE:
                g2 += net
            elif rt == _GST_FREE_REPORTING_TYPE:
                g3 += net
        # Purchase side. ``_AU_BAS_PURCHASE_TYPES`` includes ASSET so
        # capital acquisitions booked to an asset account land in G10,
        # matching au.bas_report.
        if acc_type in _AU_BAS_PURCHASE_TYPES:
            net = total_debit - total_credit
            if rt == _TAXABLE_REPORTING_TYPE:
                g11 += net
            elif rt == _CAPITAL_REPORTING_TYPE:
                # G10 is reported GST-inclusive — net + stamped GST.
                g10 += net + total_gst

    return g1, g2, g3, g10, g11


async def _bas_gst_amounts(
    session: AsyncSession,
    company_id: UUID,
    tenant_id: UUID,
    from_date: date,
    to_date: date,
) -> tuple[Decimal, Decimal]:
    """Compute (gst_on_sales, gst_on_purchases) directly from the GST
    control accounts.

    Round-2 audit fix #6: 1A/1B previously derived from G1/G11 via
    rate multiplication (g1*10%, g11/11). Reverse-calculation drifts
    from the ledger every time an invoice has a mixed-line discount,
    a manual rounding adjustment, a partial GST-free line, or a margin
    scheme — and the user files the wrong GST credits with the ATO.

    Source of truth:

    * 1A = net Cr - Dr on the configured ``gst_collected_account_code``
      (typically ``2-1310 GST Collected``) for POSTED entries in scope.
      A Cr on the liability is GST collected; a Dr is a refund/reversal.
    * 1B = net Dr - Cr on the configured ``gst_paid_account_code``
      (typically ``2-1330 GST Paid``) for POSTED entries in scope.
      A Dr is an input-tax credit accrued; a Cr is a reversal/refund.

    This approach is the same number a human BAS preparer derives — pull
    the GST Collected and GST Paid GL totals for the period — so it
    automatically handles reversed bills, journal corrections, and
    multi-line GST splits. Versus summing ``gst_amount`` on the
    income/expense legs, it nets reversals correctly (the reversal
    posts to the GST account but does not copy gst_amount onto its
    expense leg — see ``services/journal.py`` reverse path).

    If either setting is unset (small-business mode without GST
    registration), returns (0, 0) — callers should fall back to G1/G11
    only when no GST accounts exist at all.
    """
    from saebooks.services import settings as settings_svc

    collected_code = await settings_svc.get(
        session, "gst_collected_account_code", ""
    )
    paid_code = await settings_svc.get(
        session, "gst_paid_account_code", ""
    )

    # Strip JSON quoting if the setting was stored as a JSON string.
    if isinstance(collected_code, str):
        collected_code = collected_code.strip('"')
    if isinstance(paid_code, str):
        paid_code = paid_code.strip('"')

    gst_on_sales = Decimal("0")
    gst_on_purchases = Decimal("0")

    if not collected_code and not paid_code:
        return gst_on_sales, gst_on_purchases

    # One query handles both control accounts at once.
    stmt = (
        select(
            Account.code,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= from_date,
                JournalEntry.entry_date <= to_date,
                Account.code.in_([c for c in (collected_code, paid_code) if c]),
            )
        )
        .group_by(Account.code)
    )

    rows = (await session.execute(stmt)).all()
    for code, total_debit, total_credit in rows:
        dr = Decimal(str(total_debit or "0"))
        cr = Decimal(str(total_credit or "0"))
        if code == collected_code:
            # Liability account: credit-normal. 1A = net credit.
            gst_on_sales += cr - dr
        elif code == paid_code:
            # Asset/contra-liability account: debit-normal. 1B = net debit.
            gst_on_purchases += dr - cr

    return gst_on_sales, gst_on_purchases


@router.get("/bas_summary", response_model=BASSummary)
async def bas_summary(
    request: Request,
    from_date: date = Query(...),
    to_date: date = Query(...),
    registration_effective_date: date | None = Query(None),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BASSummary:
    """Australian BAS summary for a date range.

    Queries POSTED JournalLine rows for the period joined to Account
    and (outer-joined) to TaxCode via ``tax_code_id`` on the line.
    Lines without a tax code are treated as out-of-scope and contribute
    nothing to BAS buckets.

    BAS logic:
    * G1  — lines on INCOME/OTHER_INCOME accounts with reporting_type
             "taxable": net = credit - debit.
    * G3  — lines on INCOME/OTHER_INCOME accounts with reporting_type
             "gst_free": net = credit - debit.
    * G11 — lines on EXPENSE/COST_OF_SALES/OTHER_EXPENSE accounts with
             reporting_type "taxable": net = debit - credit.
    * G2  — lines on INCOME/OTHER_INCOME accounts with reporting_type
             "export": net = credit - debit.
    * G10 — lines on purchase accounts (EXPENSE/COST_OF_SALES/
             OTHER_EXPENSE/ASSET) with reporting_type "capital":
             net + gst_amount = debit - credit + GST (GST-inclusive).
    * 1A/1B come from the GST control accounts (see _bas_gst_amounts).

    G2/G10 are computed by ``_bas_aggregate`` using the SAME account-type
    sets and formulas as ``services.tax_engine.au.bas_report`` so the two
    BAS implementations reconcile by construction (C3).

    When registration_effective_date falls within the period, G1 is split:
    pre-registration sales are disclosed but excluded from 1A; only
    post-registration sales drive 1A and 1B (ATO compliance for mid-quarter
    GST registration, e.g. crossing the $75k threshold mid-BAS-period).
    """
    tenant_id = resolve_tenant_id(request)

    # Determine whether a mid-period split applies.
    _split = (
        registration_effective_date is not None
        and from_date < registration_effective_date <= to_date
    )

    g1_pre = Decimal("0")
    g1_post = Decimal("0")

    if _split:
        assert registration_effective_date is not None  # narrowing
        pre_end = registration_effective_date - timedelta(days=1)
        g1_pre, g2_pre, g3_pre, _g10_pre, _g11_pre = await _bas_aggregate(
            session, company_id, tenant_id, from_date, pre_end
        )
        g1_post, g2_post, g3_post, g10_post, g11_post = await _bas_aggregate(
            session, company_id, tenant_id, registration_effective_date, to_date
        )
        g1 = g1_pre + g1_post
        g2 = g2_pre + g2_post  # export sales disclosed in full, like G1
        g3 = g3_pre + g3_post
        g10 = g10_post  # capital ITCs only claimable from registration date
        g11 = g11_post  # ITCs only claimable from registration date
        # 1A/1B come from the actual gst_amount on the ledger lines —
        # only the post-registration slice is in scope for 1A; 1B is
        # post-only too because input-tax-credit eligibility starts on
        # the registration date.
        gst_sales, gst_purchases = await _bas_gst_amounts(
            session, company_id, tenant_id, registration_effective_date, to_date
        )
    else:
        g1, g2, g3, g10, g11 = await _bas_aggregate(
            session, company_id, tenant_id, from_date, to_date
        )
        g1_post = g1
        gst_sales, gst_purchases = await _bas_gst_amounts(
            session, company_id, tenant_id, from_date, to_date
        )

    # 1A: GST collected on sales — sum of gst_amount on POSTED INCOME lines
    # in scope. Round-2 audit fix #6: previously derived as g1 * 10% which
    # reverse-calculates GST and drifts from the actual ledger when an
    # invoice has GST-free lines, manual rounding adjustments, or any
    # mixed-treatment line.
    label_1a = gst_sales.quantize(Decimal("0.01"))

    # 1B: GST credits on purchases — sum of gst_amount on POSTED purchase
    # lines (EXPENSE/COST_OF_SALES/OTHER_EXPENSE/ASSET) in scope. Round-2
    # audit fix #6: previously derived as g11 * 1/11 which is the bug
    # critics 07 + 19 found ($141.82 off in their scenarios).
    label_1b = gst_purchases.quantize(Decimal("0.01"))

    net_gst = label_1a - label_1b

    return BASSummary(
        from_date=from_date,
        to_date=to_date,
        g1_total_sales=float(g1),
        g2_export_sales=float(g2),
        g3_other_gst_free_sales=float(g3),
        g10_capital_acquisitions=float(g10),
        g11_other_acquisitions=float(g11),
        label_1a_gst_on_sales=float(label_1a),
        label_1b_gst_on_purchases=float(label_1b),
        net_gst=float(net_gst),
        remit_or_refund="REMIT" if net_gst > Decimal("0") else "REFUND",
        registration_effective_date=registration_effective_date if _split else None,
        g1_pre_registration=float(g1_pre),
        g1_post_registration=float(g1_post),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/cashflow
# ---------------------------------------------------------------------------

# ASSET account name/code substrings used to identify cash/bank accounts
# for opening/closing cash computation (heuristic, v1).
_CASH_KEYWORDS = ("cash", "bank")


@router.get("/cashflow", response_model=CashflowStatement)
async def cashflow(
    request: Request,
    from_date: date = Query(...),
    to_date: date = Query(...),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> CashflowStatement:
    """Indirect-method cashflow statement for a date range.

    Operating section re-uses the same GL query helper as the P&L route
    to compute net_profit. Investing and financing movements are derived
    from ASSET and LIABILITY/EQUITY account movements in the GL for the
    period. Opening/closing cash is a heuristic sum of ASSET accounts
    whose name or code contains "cash" or "bank".

    TODO (v2): replace heuristic cash identification with an explicit
    ``is_cash`` flag on the Account model, and add proper adjustment
    line items (depreciation, AR/AP movement) to the operating section.
    """
    tenant_id = resolve_tenant_id(request)

    # --- Single GL query for the period covering all account types ---
    stmt = (
        select(
            Account.id,
            Account.name,
            Account.code,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= from_date,
                JournalEntry.entry_date <= to_date,
            )
        )
        .group_by(Account.id, Account.name, Account.code, Account.account_type)
        .order_by(Account.code)
    )
    period_rows = (await session.execute(stmt)).all()

    # --- Opening cash: cumulative ASSET (cash/bank) movements before from_date ---
    opening_stmt = (
        select(
            func.sum(JournalLine.debit - JournalLine.credit).label("net")
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date < from_date,
                Account.account_type == AccountType.ASSET,
            )
        )
    )
    opening_raw = (await session.execute(opening_stmt)).scalar()

    # --- Accumulate movements from period rows ---
    total_income = Decimal("0")
    total_expenses = Decimal("0")
    asset_purchases = Decimal("0")  # net debit on ASSET accounts (purchases)
    asset_disposals = Decimal("0")  # net credit on ASSET accounts (disposals)
    loan_proceeds = Decimal("0")    # net credit on LIABILITY accounts
    loan_repayments = Decimal("0")  # net debit on LIABILITY accounts
    equity_inflows = Decimal("0")   # net credit on EQUITY accounts
    equity_outflows = Decimal("0")  # net debit on EQUITY accounts
    period_cash_net = Decimal("0")  # net movement on cash/bank ASSET accounts

    for row in period_rows:
        _, acc_name, acc_code, acc_type, total_debit, total_credit = row
        total_debit = Decimal(total_debit or "0")
        total_credit = Decimal(total_credit or "0")

        if acc_type in _INCOME_TYPES:
            net = total_credit - total_debit
            if net > 0:
                total_income += net

        elif acc_type in _EXPENSE_TYPES:
            net = total_debit - total_credit
            if net > 0:
                total_expenses += net

        elif acc_type == AccountType.ASSET:
            net_debit = total_debit - total_credit
            # Identify cash/bank accounts by name or code heuristic
            name_lower = acc_name.lower()
            code_lower = acc_code.lower()
            is_cash = any(kw in name_lower or kw in code_lower for kw in _CASH_KEYWORDS)

            if is_cash:
                period_cash_net += net_debit
            else:
                # Non-cash asset: debit movement = purchase, credit = disposal
                if net_debit > 0:
                    asset_purchases += net_debit
                elif net_debit < 0:
                    asset_disposals += -net_debit  # make positive

        elif acc_type == AccountType.LIABILITY:
            net_credit = total_credit - total_debit
            if net_credit > 0:
                loan_proceeds += net_credit
            elif net_credit < 0:
                loan_repayments += -net_credit

        elif acc_type == AccountType.EQUITY:
            net_credit = total_credit - total_debit
            if net_credit > 0:
                equity_inflows += net_credit
            elif net_credit < 0:
                equity_outflows += -net_credit

    net_profit = float(total_income - total_expenses)
    total_operating = net_profit  # v1: no working-capital adjustments

    total_investing = float(asset_disposals - asset_purchases)
    total_financing = float(loan_proceeds - loan_repayments + equity_inflows - equity_outflows)

    net_change = total_operating + total_investing + total_financing

    # Opening cash: cumulative ASSET (cash/bank) debit - credit before period.
    # We use all ASSET accounts for the opening balance as a v1 simplification
    # (proper implementation needs the is_cash flag on Account).
    opening_cash = float(Decimal(str(opening_raw or "0")))
    closing_cash = opening_cash + net_change

    return CashflowStatement(
        from_date=from_date,
        to_date=to_date,
        operating=dict(
            net_profit=net_profit,
            adjustments=[],
            total_operating=total_operating,
        ),
        investing=dict(
            asset_purchases=float(-asset_purchases),  # sign: outflow is negative
            asset_disposals=float(asset_disposals),
            total_investing=total_investing,
        ),
        financing=dict(
            loan_proceeds=float(loan_proceeds),
            loan_repayments=float(-loan_repayments),  # sign: outflow is negative
            total_financing=total_financing,
        ),
        net_change=net_change,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/depreciation_schedule
# ---------------------------------------------------------------------------

# Mapping from friendly filter aliases to DB method strings.
_METHOD_ALIAS: dict[str, str] = {
    "STRAIGHT_LINE": "linear",
    "DECLINING_BALANCE": "diminishing_value",
    "linear": "linear",
    "diminishing_value": "diminishing_value",
    "no_depreciation": "no_depreciation",
}


def _useful_life_months(model: DepreciationModel) -> int:
    """Return useful life in months: method_number × method_period.

    For linear models seeded as ``method_number=N years,
    method_period=12``, this returns ``N × 12``.  For no-depreciation
    and DV models where method_number=0, returns 0.
    """
    return model.method_number * model.method_period


def _next_month_depreciation(
    model: DepreciationModel,
    current_book_value: Decimal,
    residual_value: Decimal,
    cost: Decimal,
) -> Decimal:
    """Compute one month's depreciation charge for the report column.

    STRAIGHT_LINE (linear):
        ``(cost - residual) / useful_life_months``
    DECLINING_BALANCE (diminishing_value):
        ``current_book_value × (rate_pct / 100 / 12)``
        where rate_pct comes from the model.
    Both are capped at ``max(book_value - residual, 0)`` and zeroed
    when fully depreciated.

    Returns 0 when the model is no_depreciation or when already fully
    depreciated (book_value <= residual_value).
    """
    zero = Decimal("0")
    remaining = current_book_value - residual_value
    if remaining <= 0:
        return zero

    if model.method == "linear":
        life_months = _useful_life_months(model)
        if life_months <= 0:
            return zero
        depreciable_base = cost - residual_value
        if depreciable_base <= 0:
            return zero
        charge = (depreciable_base / Decimal(life_months)).quantize(Decimal("0.01"))
        return min(charge, remaining).quantize(Decimal("0.01"))

    if model.method == "diminishing_value":
        if model.rate_pct is None or model.rate_pct <= 0:
            return zero
        monthly_rate = model.rate_pct / Decimal("100") / Decimal("12")
        charge = (current_book_value * monthly_rate).quantize(Decimal("0.01"))
        return min(charge, remaining).quantize(Decimal("0.01"))

    return zero


@router.get("/depreciation_schedule", response_model=DepreciationSchedule)
async def depreciation_schedule(
    request: Request,
    as_of_date: date | None = Query(default=None),
    method: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> DepreciationSchedule:
    """Depreciation schedule for all active (non-disposed, non-archived) assets.

    ``as_of_date`` defaults to today.  ``method`` optionally filters to a
    specific depreciation method; accepted values are ``STRAIGHT_LINE``,
    ``DECLINING_BALANCE``, ``linear``, or ``diminishing_value``.

    For each asset the report computes:

    * ``accumulated_depreciation`` — total depreciation from in_service_date
      to ``as_of_date`` via the same math used for GL posting.
    * ``current_book_value`` — ``cost - accumulated_depreciation``.
    * ``next_month_depreciation`` — one month's charge at the current
      book value; zero when ``fully_depreciated``.
    """
    as_of = as_of_date or date.today()

    # Validate and map method alias.
    db_method: str | None = None
    if method is not None:
        db_method = _METHOD_ALIAS.get(method)
        if db_method is None:
            raise HTTPException(
                422,
                f"Unknown method filter {method!r}. "
                "Use STRAIGHT_LINE, DECLINING_BALANCE, linear, or diminishing_value.",
            )

    tenant_id = resolve_tenant_id(request)

    # Build query: non-archived, non-disposed assets for this tenant+company.
    where_clauses = [
        FixedAsset.company_id == company_id,
        FixedAsset.tenant_id == tenant_id,
        FixedAsset.archived_at.is_(None),
        FixedAsset.status != "disposed",
    ]

    stmt = (
        select(FixedAsset, DepreciationModel)
        .join(
            DepreciationModel,
            FixedAsset.depreciation_model_id == DepreciationModel.id,
        )
        .where(*where_clauses)
        .order_by(FixedAsset.code)
    )
    if db_method is not None:
        stmt = stmt.where(DepreciationModel.method == db_method)

    rows = (await session.execute(stmt)).all()

    asset_lines: list[DepreciationAssetLine] = []
    total_cost = Decimal("0")
    total_accumulated = Decimal("0")
    total_book_value = Decimal("0")

    for asset, dep_model in rows:
        # Compute accumulated depreciation up to as_of.
        accum = await assets_svc.cumulative_depreciation_through(
            session, asset, as_of
        )
        book_val = (asset.cost - accum).quantize(Decimal("0.01"))
        fully_dep = book_val <= asset.residual_value

        next_month = (
            Decimal("0")
            if fully_dep
            else _next_month_depreciation(
                dep_model, book_val, asset.residual_value, asset.cost
            )
        )

        life_months = _useful_life_months(dep_model)

        asset_lines.append(
            DepreciationAssetLine(
                asset_id=asset.id,
                asset_number=asset.code,
                description=asset.description,
                acquisition_date=asset.in_service_date,
                cost=float(asset.cost),
                residual_value=float(asset.residual_value),
                useful_life_months=life_months,
                depreciation_method=dep_model.method,
                accumulated_depreciation=float(accum),
                current_book_value=float(book_val),
                next_month_depreciation=float(next_month),
                fully_depreciated=fully_dep,
            )
        )
        total_cost += asset.cost
        total_accumulated += accum
        total_book_value += book_val

    return DepreciationSchedule(
        as_of_date=as_of,
        assets=asset_lines,
        total_cost=float(total_cost),
        total_accumulated=float(total_accumulated),
        total_book_value=float(total_book_value),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/fx_revaluation — tier-5 (cycle 25)
# ---------------------------------------------------------------------------

_FX_RATE_UNAVAILABLE_NOTE = "FX rate not available — manual revaluation required"
_FX_REPORT_NOTE = "Live FX rates not configured. Amounts shown in original currency."


@router.get("/fx_revaluation", response_model=FXRevaluationReport)
async def fx_revaluation(
    request: Request,
    as_of_date: date = Query(...),
    base_currency: str = Query(default="AUD"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> FXRevaluationReport:
    """FX revaluation report as at ``as_of_date``.

    Returns all POSTED invoices and bills whose document currency differs
    from ``base_currency`` (default AUD), showing the outstanding foreign
    balance for each.  In v1 there is no live FX rate lookup; the
    ``outstanding_base`` field is always ``null`` and a note is attached
    to each item and to the report.

    Only POSTED documents are included (DRAFT uncommitted; VOIDED reversed).
    Documents with zero outstanding balance (fully paid) are included so
    the operator can confirm no residual exposure.
    """
    tenant_id = resolve_tenant_id(request)

    # --- POSTED invoices with non-base currency ---
    inv_stmt = (
        select(Invoice, Contact.name)
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(
            and_(
                Invoice.company_id == company_id,
                Invoice.tenant_id == tenant_id,
                Invoice.status == InvoiceStatus.POSTED,
                Invoice.archived_at.is_(None),
                Invoice.currency != base_currency,
                Invoice.issue_date <= as_of_date,
            )
        )
        .order_by(Invoice.issue_date, Invoice.number)
    )
    inv_rows = (await session.execute(inv_stmt)).all()

    # --- POSTED bills with non-base currency ---
    bill_stmt = (
        select(Bill, Contact.name)
        .join(Contact, Bill.contact_id == Contact.id)
        .where(
            and_(
                Bill.company_id == company_id,
                Bill.tenant_id == tenant_id,
                Bill.status == BillStatus.POSTED,
                Bill.archived_at.is_(None),
                Bill.currency != base_currency,
                Bill.issue_date <= as_of_date,
            )
        )
        .order_by(Bill.issue_date, Bill.number)
    )
    bill_rows = (await session.execute(bill_stmt)).all()

    items: list[FXRevaluationItem] = []

    for inv, contact_name in inv_rows:
        original = float(inv.total)
        paid = float(inv.amount_paid)
        outstanding = original - paid
        items.append(
            FXRevaluationItem(
                entity_type="INVOICE",
                entity_id=inv.id,
                entity_ref=inv.number,
                contact_name=contact_name,
                currency=inv.currency,
                original_amount=original,
                amount_paid=paid,
                outstanding_foreign=outstanding,
                outstanding_base=None,
                note=_FX_RATE_UNAVAILABLE_NOTE,
            )
        )

    for bill, contact_name in bill_rows:
        original = float(bill.total)
        paid = float(bill.amount_paid)
        outstanding = original - paid
        items.append(
            FXRevaluationItem(
                entity_type="BILL",
                entity_id=bill.id,
                entity_ref=bill.number,
                contact_name=contact_name,
                currency=bill.currency,
                original_amount=original,
                amount_paid=paid,
                outstanding_foreign=outstanding,
                outstanding_base=None,
                note=_FX_RATE_UNAVAILABLE_NOTE,
            )
        )

    return FXRevaluationReport(
        as_of_date=as_of_date,
        base_currency=base_currency,
        items=items,
        total_items=len(items),
        note=_FX_REPORT_NOTE,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/trial_balance — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


@router.get("/trial_balance", response_model=TrialBalanceReport)
async def trial_balance(
    request: Request,
    as_of_date: date | None = Query(default=None),
    include_zero_balance: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TrialBalanceReport:
    """Trial balance as at ``as_of_date`` (default today).

    Sums ALL POSTED JournalLine entries where ``entry_date <= as_of_date``
    (cumulative from inception), grouped by account.  Only accounts with a
    non-zero balance appear unless ``include_zero_balance=True``.

    ``balanced`` is True when ``abs(total_debits - total_credits) < 0.01``.
    """
    as_of = as_of_date or date.today()

    tenant_id = resolve_tenant_id(request)

    stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date <= as_of,
            )
        )
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )
    rows = (await session.execute(stmt)).all()

    lines: list[TrialBalanceLine] = []
    total_debits = Decimal("0")
    total_credits = Decimal("0")

    for acc_id, acc_code, acc_name, acc_type, raw_debit, raw_credit in rows:
        debit_total = Decimal(raw_debit or "0")
        credit_total = Decimal(raw_credit or "0")
        balance = debit_total - credit_total

        if not include_zero_balance and balance == Decimal("0"):
            continue

        lines.append(
            TrialBalanceLine(
                account_id=acc_id,
                code=acc_code,
                name=acc_name,
                account_type=acc_type,
                debit_total=float(debit_total),
                credit_total=float(credit_total),
                balance=float(balance),
            )
        )
        total_debits += debit_total
        total_credits += credit_total

    return TrialBalanceReport(
        as_of_date=as_of,
        accounts=lines,
        total_debits=float(total_debits),
        total_credits=float(total_credits),
        balanced=abs(total_debits - total_credits) < Decimal("0.01"),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/budget_vs_actual — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


@router.get("/budget_vs_actual", response_model=BudgetVsActualReport)
async def budget_vs_actual(
    request: Request,
    year: int = Query(...),
    month: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BudgetVsActualReport:
    """Budget vs actual for a year (or a single month within that year).

    When ``month`` is omitted the full year is aggregated (budget and
    actual are summed across all 12 months).  When ``month`` is supplied
    only that calendar month is compared.

    Accounts that have a budget row or GL activity for the period are
    included (outer join logic).  Variance = actual - budget.
    ``variance_pct`` is null when the budget is zero.
    """
    if month is not None and not 1 <= month <= 12:
        raise HTTPException(422, "month must be between 1 and 12")

    tenant_id = resolve_tenant_id(request)

    # --- Actuals ---
    actual_conditions: list[Any] = [
        JournalEntry.company_id == company_id,
        JournalEntry.tenant_id == tenant_id,
        JournalEntry.status == EntryStatus.POSTED,
        JournalEntry.archived_at.is_(None),
        func.extract("year", JournalEntry.entry_date) == year,
    ]
    if month is not None:
        actual_conditions.append(
            func.extract("month", JournalEntry.entry_date) == month
        )

    actual_stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(and_(*actual_conditions))
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )
    actual_rows = (await session.execute(actual_stmt)).all()

    # --- Budgets ---
    budget_conditions: list[Any] = [
        Budget.company_id == company_id,
        Budget.year == year,
        Budget.archived_at.is_(None),
    ]
    if month is not None:
        budget_conditions.append(Budget.month == month)

    budget_stmt = (
        select(
            Budget.account_id,
            func.sum(Budget.amount).label("total_budget"),
        )
        .where(and_(*budget_conditions))
        .group_by(Budget.account_id)
    )
    budget_rows = (await session.execute(budget_stmt)).all()

    # Resolve account metadata for budget-only accounts
    actual_account_ids = {r[0] for r in actual_rows}
    budget_account_ids = {r[0] for r in budget_rows}
    missing_ids = budget_account_ids - actual_account_ids

    meta: dict[Any, tuple[str, str, Any]] = {
        r[0]: (r[1], r[2], r[3]) for r in actual_rows
    }
    if missing_ids:
        meta_stmt = select(
            Account.id, Account.code, Account.name, Account.account_type
        ).where(Account.id.in_(missing_ids))
        for aid, code, name, atype in (await session.execute(meta_stmt)).all():
            meta[aid] = (code, name, atype)

    # Build lookup maps
    actuals_by_account: dict[Any, tuple[Decimal, Decimal]] = {
        r[0]: (Decimal(r[4] or "0"), Decimal(r[5] or "0"))
        for r in actual_rows
    }
    budgets_by_account: dict[Any, Decimal] = {
        r[0]: Decimal(r[1] or "0") for r in budget_rows
    }

    all_account_ids = sorted(
        actual_account_ids | budget_account_ids,
        key=lambda aid: meta.get(aid, ("", "", None))[0],
    )

    lines: list[BudgetVsActualLine] = []
    total_budget = Decimal("0")
    total_actual = Decimal("0")

    for aid in all_account_ids:
        if aid not in meta:
            continue
        code, name, acc_type = meta[aid]

        raw_debit, raw_credit = actuals_by_account.get(aid, (Decimal("0"), Decimal("0")))
        # Natural-sign: income = credit-debit, expenses/assets = debit-credit
        actual_val = raw_credit - raw_debit if acc_type in _INCOME_TYPES else raw_debit - raw_credit

        budget_val = budgets_by_account.get(aid, Decimal("0"))
        variance = actual_val - budget_val
        variance_pct = (
            float((variance / budget_val * 100).quantize(Decimal("0.01")))
            if budget_val != Decimal("0")
            else None
        )

        lines.append(
            BudgetVsActualLine(
                account_id=aid,
                account_code=code,
                account_name=name,
                budget=float(budget_val),
                actual=float(actual_val),
                variance=float(variance),
                variance_pct=variance_pct,
            )
        )
        total_budget += budget_val
        total_actual += actual_val

    return BudgetVsActualReport(
        year=year,
        month=month,
        lines=lines,
        total_budget=float(total_budget),
        total_actual=float(total_actual),
        total_variance=float(total_actual - total_budget),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/pl_by_segment — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


@router.get("/pl_by_segment", response_model=PLBySegmentReport)
async def pl_by_segment(
    request: Request,
    from_date: date = Query(...),
    to_date: date = Query(...),
    segment_type: str = Query(default="project"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PLBySegmentReport:
    """P&L by segment for a date range.

    Supported ``segment_type`` values: ``project``, ``department``,
    ``cost_centre``.  JournalLine is grouped by the matching dimension
    column; lines with no tag appear under the "Unassigned" segment.

    Returns HTTP 422 if an unsupported segment_type is requested.
    """
    valid = {"project", "department", "cost_centre"}
    if segment_type not in valid:
        raise HTTPException(
            422,
            {
                "status": "invalid_segment_type",
                "note": (
                    f"segment_type {segment_type!r} is not supported. "
                    f"Valid values: {sorted(valid)}"
                ),
            },
        )

    resolve_tenant_id(request)

    segment_rows = await reports_svc.pl_by_segment(
        session,
        company_id,
        from_date=from_date,
        to_date=to_date,
        segment=segment_type,
    )

    # Convert service dataclasses to API schema
    output_segments: list[PLSegmentRow] = []
    for row in segment_rows:
        sections: list[PLSegmentSection] = []
        for section in row.sections:
            section_lines = [
                PLSegmentAccountLine(
                    account_id=bal.account_id,
                    code=bal.code,
                    name=bal.name,
                    amount=float(
                        # Natural sign: income=credit-debit, expense=debit-credit
                        bal.credit - bal.debit
                        if section.account_type
                        in {AccountType.INCOME, AccountType.OTHER_INCOME}
                        else bal.debit - bal.credit
                    ),
                )
                for bal in section.rows
            ]
            section_total = sum(line.amount for line in section_lines)
            sections.append(
                PLSegmentSection(
                    account_type=section.account_type.value,
                    lines=section_lines,
                    total=section_total,
                )
            )
        output_segments.append(
            PLSegmentRow(
                segment_id=row.segment_id,
                segment_label=row.segment_label,
                sections=sections,
                net_profit=float(row.net_profit),
            )
        )

    return PLBySegmentReport(
        from_date=from_date,
        to_date=to_date,
        segment_type=segment_type,
        segments=output_segments,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/revenue_by_customer — gap PSI-2
# ---------------------------------------------------------------------------


@router.get("/revenue_by_customer", response_model=RevenueByCustomerReport)
async def revenue_by_customer(
    request: Request,
    from_date: date = Query(...),
    to_date: date = Query(...),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> RevenueByCustomerReport:
    """Revenue (ex-GST) broken down by customer for a date range.

    Uses POSTED invoices (issue_date within the window).  Returns rows
    sorted by revenue descending plus a concentration_warning flag that
    fires when the top customer accounts for >= 80 % of total revenue
    (the ATO 80/20 PSI rule threshold).
    """
    resolve_tenant_id(request)

    result = await reports_svc.revenue_by_customer(
        session,
        company_id,
        from_date=from_date,
        to_date=to_date,
    )

    total = float(result.total_revenue)
    rows = [
        RevenueByCustomerRow(
            contact_id=r.contact_id,
            contact_name=r.contact_name,
            revenue=float(r.revenue),
            pct_of_total=float(r.revenue / result.total_revenue * 100)
            if result.total_revenue > 0
            else 0.0,
        )
        for r in result.rows
    ]

    return RevenueByCustomerReport(
        from_date=from_date,
        to_date=to_date,
        rows=rows,
        total_revenue=total,
        top_customer_pct=result.top_customer_pct,
        concentration_warning=result.concentration_warning,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/ytd_turnover
# ---------------------------------------------------------------------------


@router.get("/ytd_turnover", response_model=YTDTurnoverReport)
async def ytd_turnover(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> YTDTurnoverReport:
    """YTD gross turnover for the current Australian financial year.

    Sums all INCOME and OTHER_INCOME journal credits (net of debits) for
    POSTED journal entries whose entry_date falls within the current
    Australian FY (1 July - 30 June).  Used by the dashboard to display
    the $75k GST registration threshold banner.

    Income accounts are credit-normal, so turnover = credit - debit for
    each matching journal line.  The result is always >= 0 (net credits
    cannot go negative for normal business income).

    The ``threshold_crossed`` / ``threshold_approaching`` flags only fire
    when the company is **not** already GST-registered. The 21-day-to-
    register obligation in the ATO rules only applies to businesses that
    have crossed the threshold without yet being registered; an already-
    registered business showing $75k+ in revenue is the normal case and
    must not be nagged. Suppressing both flags here is the correct fix —
    the flags drive the dashboard banners and the profit-and-loss
    threshold callout, none of which are relevant to a registered entity.
    """
    tenant_id = resolve_tenant_id(request)
    fy_start, fy_end = _current_fy_bounds()
    today = date.today()
    effective_end = min(fy_end, today)

    stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0)
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= fy_start,
                JournalEntry.entry_date <= effective_end,
                Account.account_type.in_(_INCOME_TYPES),
            )
        )
    )
    result = await session.execute(stmt)
    raw = result.scalar_one()
    ytd = max(Decimal(str(raw)), Decimal("0"))

    # Look up the company's GST status — if already registered, neither
    # threshold flag fires (the registration obligation is moot).
    company = await session.get(Company, company_id)
    already_registered = bool(company and company.gst_registered)

    _approaching_floor = _GST_THRESHOLD * Decimal("0.80")
    return YTDTurnoverReport(
        fy_start=fy_start,
        fy_end=fy_end,
        ytd_turnover=float(ytd),
        threshold=float(_GST_THRESHOLD),
        threshold_crossed=(not already_registered) and ytd >= _GST_THRESHOLD,
        threshold_approaching=(
            (not already_registered)
            and _approaching_floor <= ytd < _GST_THRESHOLD
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/statement_pack.pdf — LaTeX statement pack
# ---------------------------------------------------------------------------


def _current_fy_bounds_for_pack(today: date | None = None) -> tuple[date, date]:
    """Return (fy_start, fy_end) for the AU financial year containing today.

    Australian FY: 1 July → 30 June.  Duplicated here so this endpoint
    does not depend on the private helper defined earlier in this module.
    """
    d = today or date.today()
    if d.month >= 7:
        return date(d.year, 7, 1), date(d.year + 1, 6, 30)
    return date(d.year - 1, 7, 1), date(d.year, 6, 30)


def _subtract_one_year_pack(d: date) -> date:
    """Return the date exactly one year before ``d``, guarding 29 Feb."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        # d is 29 Feb in a leap year → prior year has no 29 Feb → use 28 Feb
        return d.replace(year=d.year - 1, day=28)


@router.get("/statement_pack.pdf", response_class=None)
async def statement_pack_pdf(
    request: Request,
    as_of_date: date | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    comparative: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> "FastAPIResponse":
    """Render a financial statement pack (P&L + Balance Sheet + Trial Balance)
    as a PDF via the LaTeX engine.

    Defaults to the current Australian financial year (1 Jul → today).
    With ``comparative=true`` (default) a prior-year column is included.

    The ctx is assembled entirely from service-layer functions — no HTTP
    self-calls.  Returns ``application/pdf`` with ``Content-Disposition: inline``.
    """
    from fastapi.responses import Response as FastAPIResponse

    from saebooks.services.latex_pdf import LatexCompileError, LatexServiceError, render_latex

    today = date.today()
    fy_start, _ = _current_fy_bounds_for_pack(today)

    as_of = as_of_date or today
    from_ = from_date or fy_start
    to_ = to_date or as_of

    tenant_id = resolve_tenant_id(request)

    # --- Company ---------------------------------------------------------
    company_obj = await session.get(Company, company_id)
    if company_obj is None:
        raise HTTPException(404, "Active company not found")

    company_ctx = {
        "name": company_obj.name,
        "legal_name": company_obj.legal_name or company_obj.name,
        "acn": company_obj.acn or "",
        "abn": company_obj.abn or "",
    }

    # --- Current period reports ------------------------------------------
    # Inline the same DB queries used by the /profit_loss, /balance_sheet,
    # and /trial_balance endpoints — no HTTP self-call.

    # P&L
    pnl_stmt = (
        select(
            Account.id,
            Account.name,
            Account.code,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date >= from_,
                JournalEntry.entry_date <= to_,
            )
        )
        .group_by(Account.id, Account.name, Account.code, Account.account_type)
        .order_by(Account.code)
    )
    pnl_rows = (await session.execute(pnl_stmt)).all()

    income_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    expenses_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for acc_id, acc_name, acc_code, acc_type, td, tc in pnl_rows:
        td = Decimal(td or "0")
        tc = Decimal(tc or "0")
        if acc_type in _INCOME_TYPES:
            net = float(tc - td)
            if net != 0.0:
                income_by_type[acc_type.value].append(
                    {"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "amount": net}
                )
        elif acc_type in _EXPENSE_TYPES:
            net = float(td - tc)
            if net != 0.0:
                expenses_by_type[acc_type.value].append(
                    {"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "amount": net}
                )

    total_income = sum(l["amount"] for lines in income_by_type.values() for l in lines)
    total_expenses = sum(l["amount"] for lines in expenses_by_type.values() for l in lines)

    pl_report: dict[str, Any] = {
        "from_date": from_.isoformat(),
        "to_date": to_.isoformat(),
        "income": {
            "INCOME": income_by_type.get("INCOME", []),
            "OTHER_INCOME": income_by_type.get("OTHER_INCOME", []),
            "total_income": total_income,
        },
        "expenses": {
            "EXPENSE": expenses_by_type.get("EXPENSE", []),
            "COST_OF_SALES": expenses_by_type.get("COST_OF_SALES", []),
            "OTHER_EXPENSE": expenses_by_type.get("OTHER_EXPENSE", []),
            "total_expenses": total_expenses,
        },
        "net_profit": total_income - total_expenses,
    }

    # Balance Sheet
    bs_stmt = (
        select(
            Account.id,
            Account.name,
            Account.code,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date <= as_of,
            )
        )
        .group_by(Account.id, Account.name, Account.code, Account.account_type)
        .order_by(Account.code)
    )
    bs_rows = (await session.execute(bs_stmt)).all()

    assets: list[dict[str, Any]] = []
    liabilities: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    cye_income_credit = Decimal("0")
    cye_expense_debit = Decimal("0")

    for acc_id, acc_name, acc_code, acc_type, td, tc in bs_rows:
        td = Decimal(td or "0")
        tc = Decimal(tc or "0")
        if acc_type in _ASSET_TYPES:
            bal = float(td - tc)
            if bal != 0.0:
                assets.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
        elif acc_type in _LIABILITY_TYPES:
            bal = float(tc - td)
            if bal != 0.0:
                liabilities.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
        elif acc_type in _EQUITY_TYPES:
            bal = float(tc - td)
            if bal != 0.0:
                equity_rows.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
        elif acc_type in _INCOME_TYPES:
            cye_income_credit += tc - td
        elif acc_type in _EXPENSE_TYPES:
            cye_expense_debit += td - tc

    cye_balance = float(cye_income_credit - cye_expense_debit)
    equity_rows.append({"account_id": "00000000-0000-0000-0000-000000000000", "account_name": "Current Year Earnings", "code": "CYE", "balance": cye_balance})

    total_assets = sum(l["balance"] for l in assets)
    total_liabilities = sum(l["balance"] for l in liabilities)
    total_equity = sum(l["balance"] for l in equity_rows)
    bs_difference = abs(total_assets - total_liabilities - total_equity)

    bs_report: dict[str, Any] = {
        "as_of_date": as_of.isoformat(),
        "assets": {"ASSET": assets, "total_assets": total_assets},
        "liabilities": {"LIABILITY": liabilities, "total_liabilities": total_liabilities},
        "equity": {"EQUITY": equity_rows, "total_equity": total_equity},
        "balanced": bs_difference < 0.01,
        "difference": round(bs_difference, 2),
    }

    # Trial Balance
    tb_stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            and_(
                JournalEntry.company_id == company_id,
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.archived_at.is_(None),
                JournalEntry.entry_date <= as_of,
            )
        )
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )
    tb_rows = (await session.execute(tb_stmt)).all()

    tb_lines = []
    tb_total_debits = Decimal("0")
    tb_total_credits = Decimal("0")
    for acc_id, acc_code, acc_name, acc_type, td, tc in tb_rows:
        td = Decimal(td or "0")
        tc = Decimal(tc or "0")
        balance = td - tc
        if balance == Decimal("0"):
            continue
        tb_lines.append({
            "account_id": str(acc_id),
            "code": acc_code,
            "name": acc_name,
            "account_type": acc_type.value,
            "debit_total": float(td),
            "credit_total": float(tc),
            "balance": float(balance),
        })
        tb_total_debits += td
        tb_total_credits += tc

    tb_report: dict[str, Any] = {
        "as_of_date": as_of.isoformat(),
        "accounts": tb_lines,
        "total_debits": float(tb_total_debits),
        "total_credits": float(tb_total_credits),
        "balanced": abs(tb_total_debits - tb_total_credits) < Decimal("0.01"),
    }

    # --- Prior-year comparatives ------------------------------------------
    comp_pl: dict[str, Any] = {}
    comp_bs: dict[str, Any] = {}
    prior_from = _subtract_one_year_pack(from_)
    prior_to = _subtract_one_year_pack(to_)
    prior_as_of = _subtract_one_year_pack(as_of)

    if comparative:
        # Prior P&L
        prior_pnl_stmt = (
            select(
                Account.id,
                Account.name,
                Account.code,
                Account.account_type,
                func.sum(JournalLine.debit).label("total_debit"),
                func.sum(JournalLine.credit).label("total_credit"),
            )
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .join(Account, JournalLine.account_id == Account.id)
            .where(
                and_(
                    JournalEntry.company_id == company_id,
                    JournalEntry.tenant_id == tenant_id,
                    JournalEntry.status == EntryStatus.POSTED,
                    JournalEntry.archived_at.is_(None),
                    JournalEntry.entry_date >= prior_from,
                    JournalEntry.entry_date <= prior_to,
                )
            )
            .group_by(Account.id, Account.name, Account.code, Account.account_type)
            .order_by(Account.code)
        )
        prior_pnl_rows = (await session.execute(prior_pnl_stmt)).all()

        prior_income_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        prior_expenses_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for acc_id, acc_name, acc_code, acc_type, td, tc in prior_pnl_rows:
            td = Decimal(td or "0")
            tc = Decimal(tc or "0")
            if acc_type in _INCOME_TYPES:
                net = float(tc - td)
                if net != 0.0:
                    prior_income_by_type[acc_type.value].append(
                        {"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "amount": net}
                    )
            elif acc_type in _EXPENSE_TYPES:
                net = float(td - tc)
                if net != 0.0:
                    prior_expenses_by_type[acc_type.value].append(
                        {"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "amount": net}
                    )

        p_total_income = sum(l["amount"] for lines in prior_income_by_type.values() for l in lines)
        p_total_expenses = sum(l["amount"] for lines in prior_expenses_by_type.values() for l in lines)

        prior_pl: dict[str, Any] = {
            "income": {
                "INCOME": prior_income_by_type.get("INCOME", []),
                "OTHER_INCOME": prior_income_by_type.get("OTHER_INCOME", []),
                "total_income": p_total_income,
            },
            "expenses": {
                "EXPENSE": prior_expenses_by_type.get("EXPENSE", []),
                "COST_OF_SALES": prior_expenses_by_type.get("COST_OF_SALES", []),
                "OTHER_EXPENSE": prior_expenses_by_type.get("OTHER_EXPENSE", []),
                "total_expenses": p_total_expenses,
            },
            "net_profit": p_total_income - p_total_expenses,
        }

        # Merge current + prior P&L into comp_pl using the same logic as
        # saebooks_web._build_comparative_pl — align by account_id.
        def _merge_lines(current_lines: list[dict], prior_lines: list[dict]) -> list[dict]:
            prior_by_id = {l["account_id"]: l for l in prior_lines if l.get("account_id")}
            merged = []
            for line in current_lines:
                aid = line.get("account_id")
                prior = prior_by_id.pop(aid, {})
                merged.append({
                    "account_id": aid,
                    "account_name": line.get("account_name", ""),
                    "code": line.get("code", ""),
                    "current_amount": float(line.get("amount", line.get("balance", 0)) or 0),
                    "prior_amount": float(prior.get("amount", prior.get("balance", 0)) or 0),
                })
            for aid, line in prior_by_id.items():
                merged.append({
                    "account_id": aid,
                    "account_name": line.get("account_name", ""),
                    "code": line.get("code", ""),
                    "current_amount": 0.0,
                    "prior_amount": float(line.get("amount", line.get("balance", 0)) or 0),
                })
            return merged

        c_income = pl_report["income"]
        p_income = prior_pl["income"]
        c_expenses = pl_report["expenses"]
        p_expenses = prior_pl["expenses"]

        comp_pl = {
            "income": {
                "INCOME": _merge_lines(c_income.get("INCOME", []), p_income.get("INCOME", [])),
                "OTHER_INCOME": _merge_lines(c_income.get("OTHER_INCOME", []), p_income.get("OTHER_INCOME", [])),
                "total_income_current": float(c_income.get("total_income", 0) or 0),
                "total_income_prior": float(p_income.get("total_income", 0) or 0),
            },
            "expenses": {
                "EXPENSE": _merge_lines(c_expenses.get("EXPENSE", []), p_expenses.get("EXPENSE", [])),
                "COST_OF_SALES": _merge_lines(c_expenses.get("COST_OF_SALES", []), p_expenses.get("COST_OF_SALES", [])),
                "OTHER_EXPENSE": _merge_lines(c_expenses.get("OTHER_EXPENSE", []), p_expenses.get("OTHER_EXPENSE", [])),
                "total_expenses_current": float(c_expenses.get("total_expenses", 0) or 0),
                "total_expenses_prior": float(p_expenses.get("total_expenses", 0) or 0),
            },
            "net_profit_current": float(pl_report["net_profit"]),
            "net_profit_prior": float(prior_pl["net_profit"]),
        }

        # Prior Balance Sheet
        prior_bs_stmt = (
            select(
                Account.id,
                Account.name,
                Account.code,
                Account.account_type,
                func.sum(JournalLine.debit).label("total_debit"),
                func.sum(JournalLine.credit).label("total_credit"),
            )
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .join(Account, JournalLine.account_id == Account.id)
            .where(
                and_(
                    JournalEntry.company_id == company_id,
                    JournalEntry.tenant_id == tenant_id,
                    JournalEntry.status == EntryStatus.POSTED,
                    JournalEntry.archived_at.is_(None),
                    JournalEntry.entry_date <= prior_as_of,
                )
            )
            .group_by(Account.id, Account.name, Account.code, Account.account_type)
            .order_by(Account.code)
        )
        prior_bs_rows = (await session.execute(prior_bs_stmt)).all()

        p_assets: list[dict[str, Any]] = []
        p_liabilities: list[dict[str, Any]] = []
        p_equity_rows: list[dict[str, Any]] = []
        p_cye_income_credit = Decimal("0")
        p_cye_expense_debit = Decimal("0")

        for acc_id, acc_name, acc_code, acc_type, td, tc in prior_bs_rows:
            td = Decimal(td or "0")
            tc = Decimal(tc or "0")
            if acc_type in _ASSET_TYPES:
                bal = float(td - tc)
                if bal != 0.0:
                    p_assets.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
            elif acc_type in _LIABILITY_TYPES:
                bal = float(tc - td)
                if bal != 0.0:
                    p_liabilities.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
            elif acc_type in _EQUITY_TYPES:
                bal = float(tc - td)
                if bal != 0.0:
                    p_equity_rows.append({"account_id": str(acc_id), "account_name": acc_name, "code": acc_code, "balance": bal})
            elif acc_type in _INCOME_TYPES:
                p_cye_income_credit += tc - td
            elif acc_type in _EXPENSE_TYPES:
                p_cye_expense_debit += td - tc

        p_cye = float(p_cye_income_credit - p_cye_expense_debit)
        p_equity_rows.append({"account_id": "00000000-0000-0000-0000-000000000000", "account_name": "Current Year Earnings", "code": "CYE", "balance": p_cye})

        p_total_assets = sum(l["balance"] for l in p_assets)
        p_total_liabilities = sum(l["balance"] for l in p_liabilities)
        p_total_equity = sum(l["balance"] for l in p_equity_rows)

        prior_bs: dict[str, Any] = {
            "assets": {"ASSET": p_assets, "total_assets": p_total_assets},
            "liabilities": {"LIABILITY": p_liabilities, "total_liabilities": p_total_liabilities},
            "equity": {"EQUITY": p_equity_rows, "total_equity": p_total_equity},
        }

        comp_bs = {
            "assets": {
                "ASSET": _merge_lines(bs_report["assets"]["ASSET"], prior_bs["assets"]["ASSET"]),
                "total_assets_current": total_assets,
                "total_assets_prior": p_total_assets,
            },
            "liabilities": {
                "LIABILITY": _merge_lines(bs_report["liabilities"]["LIABILITY"], prior_bs["liabilities"]["LIABILITY"]),
                "total_liabilities_current": total_liabilities,
                "total_liabilities_prior": p_total_liabilities,
            },
            "equity": {
                "EQUITY": _merge_lines(equity_rows, p_equity_rows),
                "total_equity_current": total_equity,
                "total_equity_prior": p_total_equity,
            },
            "balanced": bs_difference < 0.01,
            "difference": round(bs_difference, 2),
        }

    # --- Assemble ctx and render ------------------------------------------
    ctx = {
        "company": company_ctx,
        "from_date": from_.isoformat(),
        "to_date": to_.isoformat(),
        "as_of_date": as_of.isoformat(),
        "prepared": today.isoformat(),
        "pl_report": pl_report,
        "bs_report": bs_report,
        "tb_report": tb_report,
        "comparative": comparative,
        "comp_pl": comp_pl,
        "comp_bs": comp_bs,
        "prior_from": prior_from.isoformat(),
        "prior_to": prior_to.isoformat(),
        "prior_as_of": prior_as_of.isoformat(),
    }

    try:
        pdf_bytes = await render_latex("statement_pack", ctx)
    except LatexCompileError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LaTeX compile error: {exc.log_tail}",
        ) from exc
    except LatexServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LaTeX service error: {exc}",
        ) from exc

    return FastAPIResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="statement-pack.pdf"'},
    )
