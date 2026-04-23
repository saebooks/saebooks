"""JSON router — ``/api/v1/reports``.

Tier-5 report endpoints: Aged Receivables and Aged Payables.

Both reports walk the ``invoices`` / ``bills`` tables directly (not the
GL) so they can show per-document outstanding balances.  The outstanding
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
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import AgedReport
from saebooks.db import AsyncSessionLocal
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus

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
