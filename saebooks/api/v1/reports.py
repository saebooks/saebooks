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
from saebooks.models.contact import Contact
from saebooks.models.depreciation_model import DepreciationModel
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.invoice import Invoice, InvoiceStatus
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
) -> AgedReport:
    """Assemble an AgedReport from DB rows."""
    zero = Decimal("0")

    # contact_id → {"contact_id": ..., "contact_name": ..., <bucket>: ...}
    groups: dict[UUID, dict[str, Any]] = {}

    for doc, contact_name in rows:
        contact_id: UUID = doc.contact_id
        balance: Decimal = doc.total - doc.amount_paid
        days_overdue: int = (as_of - doc.due_date).days
        label = _days_to_bucket(days_overdue, bucket_days)

        if contact_id not in groups:
            groups[contact_id] = {
                "contact_id": str(contact_id),
                "contact_name": contact_name,
                **{lbl: zero for lbl in bucket_labels},
                "total": zero,
            }

        groups[contact_id][label] = groups[contact_id][label] + balance
        groups[contact_id]["total"] = groups[contact_id]["total"] + balance

    # Sort by total descending
    sorted_groups = sorted(
        groups.values(), key=lambda g: g["total"], reverse=True
    )

    # Grand totals
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

    return AgedReport(
        as_of_date=as_of,
        buckets=bucket_labels,
        contacts=[_floatify(g) for g in sorted_groups],
        totals=_floatify(totals),
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

    return _build_report(rows, as_of, bd, labels)


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
) -> tuple[Decimal, Decimal, Decimal]:
    """Aggregate G1, G3, G11 totals for POSTED lines in [from_date, to_date]."""
    stmt = (
        select(
            Account.account_type,
            TaxCode.reporting_type,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
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
    g3 = Decimal("0")
    g11 = Decimal("0")

    for acc_type, reporting_type, total_debit, total_credit in rows:
        total_debit = Decimal(total_debit or "0")
        total_credit = Decimal(total_credit or "0")
        rt = reporting_type or ""

        if acc_type in _INCOME_TYPES:
            net = total_credit - total_debit
            if rt == _TAXABLE_REPORTING_TYPE:
                g1 += net
            elif rt == _GST_FREE_REPORTING_TYPE:
                g3 += net
        elif acc_type in _EXPENSE_TYPES:
            net = total_debit - total_credit
            if rt == _TAXABLE_REPORTING_TYPE:
                g11 += net

    return g1, g3, g11


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
    * 1A  = G1 × 10%  (GST collected, calculated from GST-exclusive base).
    * 1B  = G11 × 1/11 (GST credits, tax-inclusive component of purchase).
    * G2/G10 are always 0 in v1 (no export or capital acquisition tracking).

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
        g1_pre, g3_pre, _g11_pre = await _bas_aggregate(
            session, company_id, tenant_id, from_date, pre_end
        )
        g1_post, g3_post, g11_post = await _bas_aggregate(
            session, company_id, tenant_id, registration_effective_date, to_date
        )
        g1 = g1_pre + g1_post
        g3 = g3_pre + g3_post
        g11 = g11_post  # ITCs only claimable from registration date
    else:
        g1, g3, g11 = await _bas_aggregate(
            session, company_id, tenant_id, from_date, to_date
        )
        g1_post = g1

    # 1A: GST collected on taxable sales (10% of GST-exclusive base).
    # Only post-registration sales attract GST when a split applies.
    label_1a = (g1_post * _GST_RATE).quantize(Decimal("0.01"))

    # 1B: GST credits on taxable purchases (1/11 of GST-inclusive amount).
    label_1b = (g11 * _GST_INCLUSIVE_FRACTION).quantize(Decimal("0.01"))

    net_gst = label_1a - label_1b

    return BASSummary(
        from_date=from_date,
        to_date=to_date,
        g1_total_sales=float(g1),
        g2_export_sales=0.0,
        g3_other_gst_free_sales=float(g3),
        g10_capital_acquisitions=0.0,
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

    _approaching_floor = _GST_THRESHOLD * Decimal("0.80")
    return YTDTurnoverReport(
        fy_start=fy_start,
        fy_end=fy_end,
        ytd_turnover=float(ytd),
        threshold=float(_GST_THRESHOLD),
        threshold_crossed=ytd >= _GST_THRESHOLD,
        threshold_approaching=_approaching_floor <= ytd < _GST_THRESHOLD,
    )
