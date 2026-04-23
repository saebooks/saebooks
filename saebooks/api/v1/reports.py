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
from saebooks.api.v1.schemas import AgedReport, BSReport, PnLReport
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

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
