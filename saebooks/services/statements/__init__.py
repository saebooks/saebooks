"""Customer statement aggregation + supplier statement reconciliation service layer.

AR customer statements (used by contacts API):
    GET  /api/v1/contacts/{id}/statement
    GET  /api/v1/contacts/{id}/statement.pdf

Supplier statement reconciliation (Phase 1, #28):
    import from saebooks.services.statements.extract
    import from saebooks.services.statements.reconcile
    import from saebooks.services.statements.ingest
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.payment import Payment, PaymentDirection


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


async def render_statement_pdf(statement: Statement, *, company: Any) -> bytes:
    """Render a Statement object to a PDF via the LaTeX engine.

    Builds a context dict from *statement* and *company*, then delegates to
    ``render_latex("contact_statement", ctx)`` which POSTs to latex-api.

    Raises
    ------
    LatexCompileError
        When xelatex fails.
    LatexServiceError
        On connection failure or unexpected latex-api response.
    """
    from saebooks.services.latex_pdf import render_latex

    company_abn = getattr(company, "abn", "") or "" if company else ""
    company_acn = getattr(company, "acn", "") or "" if company else ""
    company_legal_name = (
        getattr(company, "legal_name", None) or getattr(company, "name", "")
    ) if company else ""

    # Address is stored as a dict on the company model (address column = JSON).
    company_address: dict[str, str] = {}
    raw_addr = getattr(company, "address", None) if company else None
    if isinstance(raw_addr, dict):
        company_address = raw_addr

    ctx: dict[str, Any] = {
        "contact_name": statement.contact_name,
        "period_start":  statement.period_start.isoformat(),
        "period_end":    statement.period_end.isoformat(),
        "company": {
            "legal_name": company_legal_name,
            "abn":        company_abn,
            "acn":        company_acn,
            "address":    company_address,
        },
        "contact": {
            "name":  statement.contact_name,
            "email": statement.contact_email or "",
        },
        "opening_balance":            str(statement.opening_balance),
        "total_invoiced_in_period":   str(statement.total_invoiced_in_period),
        "total_paid_in_period":       str(statement.total_paid_in_period),
        "closing_balance":            str(statement.closing_balance),
        "lines": [
            {
                "line_date":   ln.line_date.isoformat(),
                "kind":        ln.kind.capitalize(),
                "reference":   ln.reference,
                "description": (ln.description or "")[:80],
                "amount_dr":   str(ln.amount_dr),
                "amount_cr":   str(ln.amount_cr),
                "balance":     str(ln.balance),
            }
            for ln in statement.lines
        ],
    }
    return await render_latex("contact_statement", ctx)
