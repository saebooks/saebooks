"""TPAR (Taxable Payments Annual Report) service.

Generates a summary of all POSTED bills paid to contacts flagged as
is_tpar_supplier, scoped to a financial year period.  The ATO threshold
for civil contractors is $20k ex-GST per payee per year — we include all
payees regardless and let the caller filter if desired.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact


@dataclass
class TparPayee:
    contact_id: uuid.UUID
    contact_name: str
    abn: str | None
    total_excl_gst: Decimal
    total_gst: Decimal
    total_incl_gst: Decimal


@dataclass
class TparReport:
    from_date: date
    to_date: date
    payees: list[TparPayee]
    grand_total_excl_gst: Decimal
    grand_total_gst: Decimal
    grand_total_incl_gst: Decimal


_THRESHOLD = Decimal("20000.00")


async def tpar_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> TparReport:
    """Return TPAR payee list for the period.

    Uses bill issue_date to assign the payment to a period (consistent
    with how BAS and aged-AP work).  Only POSTED bills against contacts
    with is_tpar_supplier=True are included.
    """
    today = date.today()
    fd = from_date or date(today.year - 1, 7, 1)
    td = to_date or date(today.year, 6, 30)

    # Fetch TPAR-flagged contacts for this company
    tpar_contacts = (
        await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.is_tpar_supplier.is_(True),
                Contact.archived_at.is_(None),
            )
        )
    ).scalars().all()

    tpar_ids = {c.id for c in tpar_contacts}
    contact_by_id = {c.id: c for c in tpar_contacts}

    if not tpar_ids:
        return TparReport(
            from_date=fd,
            to_date=td,
            payees=[],
            grand_total_excl_gst=Decimal("0"),
            grand_total_gst=Decimal("0"),
            grand_total_incl_gst=Decimal("0"),
        )

    bills = (
        await session.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.contact_id.in_(tpar_ids),
                Bill.status == BillStatus.POSTED,
                Bill.issue_date >= fd,
                Bill.issue_date <= td,
                Bill.archived_at.is_(None),
            )
        )
    ).scalars().all()

    # Aggregate per contact using base (AUD) amounts
    totals: dict[uuid.UUID, list[Decimal]] = {}
    for bill in bills:
        if bill.contact_id not in totals:
            totals[bill.contact_id] = [Decimal("0"), Decimal("0"), Decimal("0")]
        totals[bill.contact_id][0] += bill.base_subtotal
        totals[bill.contact_id][1] += bill.base_tax_total
        totals[bill.contact_id][2] += bill.base_total

    payees = [
        TparPayee(
            contact_id=cid,
            contact_name=contact_by_id[cid].name,
            abn=contact_by_id[cid].abn,
            total_excl_gst=sums[0],
            total_gst=sums[1],
            total_incl_gst=sums[2],
        )
        for cid, sums in sorted(totals.items(), key=lambda kv: contact_by_id[kv[0]].name)
    ]

    return TparReport(
        from_date=fd,
        to_date=td,
        payees=payees,
        grand_total_excl_gst=sum((p.total_excl_gst for p in payees), Decimal("0")),
        grand_total_gst=sum((p.total_gst for p in payees), Decimal("0")),
        grand_total_incl_gst=sum((p.total_incl_gst for p in payees), Decimal("0")),
    )
