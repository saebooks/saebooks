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
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import AgedReport, BSReport, BASSummary, CashflowStatement, PnLReport
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession) -> UUID:
    """Return the first active company — phase-1 single-company assumption."""
    result = await session.execute(
        select(Company)
        .where(Company.archived_at.is_(None))
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company.id


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
    as_of_date: date | None = Query(default=None),
    bucket_days: list[int] = Query(default=[0, 30, 60, 90]),
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

    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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
    as_of_date: date | None = Query(default=None),
    bucket_days: list[int] = Query(default=[0, 30, 60, 90]),
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

    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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
    from_date: date = Query(...),
    to_date: date = Query(...),
    include_draft: bool = Query(default=False),
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

    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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
    as_of_date: date = Query(...),
) -> BSReport:
    """Balance sheet as at ``as_of_date``.

    Sums ALL POSTED JournalLine entries where ``entry_date <= as_of_date``
    (cumulative from inception).

    Asset accounts: balance = debit - credit.
    Liability + equity accounts: balance = credit - debit.
    Accounts with a zero net balance are omitted from the response.

    ``balanced`` is True when
    ``abs(total_assets - total_liabilities - total_equity) < 0.01``.
    """
    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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


@router.get("/bas_summary", response_model=BASSummary)
async def bas_summary(
    from_date: date = Query(...),
    to_date: date = Query(...),
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
    """
    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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
        rt = reporting_type or ""  # None when no tax code attached — skip

        if acc_type in _INCOME_TYPES:
            net = total_credit - total_debit  # income is credit-normal
            if rt == _TAXABLE_REPORTING_TYPE:
                g1 += net
            elif rt == _GST_FREE_REPORTING_TYPE:
                g3 += net
        elif acc_type in _EXPENSE_TYPES:
            net = total_debit - total_credit  # expenses are debit-normal
            if rt == _TAXABLE_REPORTING_TYPE:
                g11 += net

    # 1A: GST collected on taxable sales (10% of GST-exclusive base).
    label_1a = (g1 * _GST_RATE).quantize(Decimal("0.01"))

    # 1B: GST credits on taxable purchases (1/11 of GST-inclusive amount).
    # G11 represents the gross (GST-inclusive) purchase amount on the
    # expense line.  The embedded GST component is gross × 1/11.
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
    )


# ---------------------------------------------------------------------------
# GET /api/v1/reports/cashflow
# ---------------------------------------------------------------------------

# ASSET account name/code substrings used to identify cash/bank accounts
# for opening/closing cash computation (heuristic, v1).
_CASH_KEYWORDS = ("cash", "bank")


@router.get("/cashflow", response_model=CashflowStatement)
async def cashflow(
    from_date: date = Query(...),
    to_date: date = Query(...),
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
    async with AsyncSessionLocal() as session:
        tenant_id = resolve_tenant_id()
        company_id = await _first_company_id(session)

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
        acc_id, acc_name, acc_code, acc_type, total_debit, total_credit = row
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
