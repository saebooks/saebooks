"""Customer statement aggregation.

A statement is the AR per-customer activity for a date range:

* Opening balance — sum of (invoice.total - amount_paid) for any POSTED
  invoice issued BEFORE the period_start that's still partly or fully
  unpaid (or all if the contact only has outstanding invoices in the
  window — opening is 0 then).
* Lines in the period — POSTED invoices issued in [from, to] and
  INCOMING payments allocated to those invoices in the same window.
  Ordered by date, then by kind (invoice first), then by number.
* Closing balance — opening + sum(invoice totals in period) - sum(payments in period).

The aggregation is read-only and tenant-scoped via the shared session.

Used by:
    GET  /api/v1/contacts/{id}/statement
    GET  /api/v1/contacts/{id}/statement.pdf
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.payment import Payment, PaymentAllocation, PaymentDirection


@dataclass
class StatementLine:
    line_date: date
    kind: str          # "invoice" | "payment"
    reference: str     # invoice number or payment number
    description: str   # short label for the row
    amount_dr: Decimal # invoice total (debits AR)
    amount_cr: Decimal # payment allocated (credits AR)
    balance: Decimal   # running closing balance after this line


@dataclass
class Statement:
    contact_id: uuid.UUID
    contact_name: str
    contact_email: str | None
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    total_invoiced_in_period: Decimal
    total_paid_in_period: Decimal
    lines: list[StatementLine] = field(default_factory=list)


async def build_statement(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> Statement:
    """Build a customer statement for the given contact and date range.

    Caller is responsible for setting ``app.current_tenant`` on the
    session (the shared get_session dep does this from JWT claims).
    """
    # Resolve contact (RLS will block cross-tenant).
    contact = (
        await session.execute(
            select(Contact).where(
                Contact.id == contact_id,
                Contact.company_id == company_id,
                Contact.tenant_id == tenant_id,
            )
        )
    ).scalars().first()
    if contact is None:
        raise ValueError(f"Contact {contact_id} not found in this company/tenant")

    # Opening: sum of (total - amount_paid) for POSTED invoices issued BEFORE the period_start.
    opening_q = select(
        func.coalesce(func.sum(Invoice.total - Invoice.amount_paid), Decimal("0"))
    ).where(
        Invoice.company_id == company_id,
        Invoice.tenant_id == tenant_id,
        Invoice.contact_id == contact_id,
        Invoice.status == InvoiceStatus.POSTED,
        Invoice.issue_date < period_start,
        Invoice.archived_at.is_(None),
    )
    opening_balance = (await session.execute(opening_q)).scalar_one() or Decimal("0")

    # Invoices in period.
    invoices_q = (
        select(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.tenant_id == tenant_id,
            Invoice.contact_id == contact_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.issue_date >= period_start,
            Invoice.issue_date <= period_end,
            Invoice.archived_at.is_(None),
        )
        .order_by(Invoice.issue_date, Invoice.number)
    )
    invoices = (await session.execute(invoices_q)).scalars().all()

    # Payments (INCOMING) from this contact in the period, allocated to ANY
    # invoice of theirs. We surface the payment itself; the per-invoice
    # apportionment is in payment_allocations.
    payments_q = (
        select(Payment)
        .where(
            Payment.company_id == company_id,
            Payment.tenant_id == tenant_id,
            Payment.contact_id == contact_id,
            Payment.direction == PaymentDirection.INCOMING,
            Payment.payment_date >= period_start,
            Payment.payment_date <= period_end,
        )
        .order_by(Payment.payment_date, Payment.number)
    )
    payments = (await session.execute(payments_q)).scalars().all()

    # Build chronological line list; for same-day, invoices precede payments
    # so the balance shows "raised then paid" rather than negative-then-flat.
    timeline: list[tuple[date, int, object]] = []
    for inv in invoices:
        timeline.append((inv.issue_date, 0, inv))
    for pay in payments:
        timeline.append((pay.payment_date, 1, pay))
    timeline.sort(key=lambda t: (t[0], t[1]))

    lines: list[StatementLine] = []
    running = opening_balance
    total_invoiced = Decimal("0")
    total_paid = Decimal("0")

    for row_date, _kind_order, obj in timeline:
        if isinstance(obj, Invoice):
            amt = obj.total
            running += amt
            total_invoiced += amt
            lines.append(StatementLine(
                line_date=row_date,
                kind="invoice",
                reference=obj.number or str(obj.id)[:8],
                description=(obj.notes or "")[:80] or f"Invoice {obj.number or ''}",
                amount_dr=amt,
                amount_cr=Decimal("0"),
                balance=running,
            ))
        else:  # Payment
            amt = obj.amount
            running -= amt
            total_paid += amt
            lines.append(StatementLine(
                line_date=row_date,
                kind="payment",
                reference=obj.number or str(obj.id)[:8],
                description=(obj.reference or obj.notes or "")[:80] or "Payment received",
                amount_dr=Decimal("0"),
                amount_cr=amt,
                balance=running,
            ))

    return Statement(
        contact_id=contact_id,
        contact_name=contact.name,
        contact_email=contact.email,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=running,
        total_invoiced_in_period=total_invoiced,
        total_paid_in_period=total_paid,
        lines=lines,
    )


def render_statement_pdf(statement: Statement, *, company: Any) -> bytes:
    """Render a Statement object to a PDF using the same primitives as
    quotes/invoices/bills. Layout: header (kind=Statement, period),
    parties (from=company, to=contact), totals (opening, invoiced,
    paid, closing), then a chronological lines table.
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    )
    from saebooks.services.pdf import _styles, _address_block

    styles = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Statement {statement.contact_name}",
    )
    story: list[Any] = []

    # Header
    header_rows = [[
        Paragraph("STATEMENT", styles["h1"]),
        Paragraph(
            f"<b>Period</b> {statement.period_start.isoformat()} → "
            f"{statement.period_end.isoformat()}",
            styles["right"],
        ),
    ]]
    header_tbl = Table(header_rows, colWidths=[95 * mm, 85 * mm])
    header_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header_tbl)
    story.append(Spacer(1, 8 * mm))

    # Parties — FROM company, TO contact
    company_block = {
        "name": (getattr(company, "legal_name", None) or getattr(company, "name", "")) if company else "",
        "abn":  getattr(company, "abn", "") if company else "",
    }
    if company and getattr(company, "address", None):
        company_block.update(company.address)
    contact_block = {
        "name":  statement.contact_name,
        "email": statement.contact_email or "",
    }
    from_col = [Paragraph("FROM", styles["h2"]), *_address_block(company_block, styles)]
    to_col   = [Paragraph("TO",   styles["h2"]), *_address_block(contact_block, styles)]
    parties = Table([[from_col, to_col]], colWidths=[90 * mm, 90 * mm])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(parties)
    story.append(Spacer(1, 8 * mm))

    # Totals box
    tot_rows = [
        ["Opening balance", f"${statement.opening_balance:,.2f}"],
        ["Invoiced this period", f"${statement.total_invoiced_in_period:,.2f}"],
        ["Paid this period", f"-${statement.total_paid_in_period:,.2f}"],
        ["Closing balance", f"${statement.closing_balance:,.2f}"],
    ]
    tot_tbl = Table(tot_rows, colWidths=[120 * mm, 60 * mm])
    tot_tbl.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, (0, 0, 0)),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(tot_tbl)
    story.append(Spacer(1, 6 * mm))

    # Lines table
    line_rows: list[list[Any]] = [
        ["Date", "Type", "Reference", "Description", "Debit", "Credit", "Balance"]
    ]
    for ln in statement.lines:
        line_rows.append([
            ln.line_date.isoformat(),
            ln.kind.capitalize(),
            ln.reference,
            (ln.description or "")[:40],
            f"${ln.amount_dr:,.2f}" if ln.amount_dr else "",
            f"${ln.amount_cr:,.2f}" if ln.amount_cr else "",
            f"${ln.balance:,.2f}",
        ])
    lines_tbl = Table(
        line_rows,
        colWidths=[22 * mm, 18 * mm, 24 * mm, 56 * mm, 20 * mm, 20 * mm, 22 * mm],
        repeatRows=1,
    )
    lines_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), (0.92, 0.92, 0.92)),
        ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN",     (4, 0), (-1, -1), "RIGHT"),
        ("GRID",      (0, 0), (-1, -1), 0.25, (0.7, 0.7, 0.7)),
        ("FONTSIZE",  (0, 0), (-1, -1), 8),
    ]))
    story.append(lines_tbl)

    doc.build(story)
    return buf.getvalue()
