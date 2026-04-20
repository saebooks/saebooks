"""AR invoice service — create, update, post, void, mark-sent.

All GL-impacting operations go through ``services/journal.py`` —
invoices never touch ``journal_entries`` directly. GST auto-posting
is already wired up in ``gst.py``: a line with ``tax_code_id`` +
``gst_amount`` on an INCOME account gets a matching CR GST Collected
appended during post.

Numbers come from ``services/numbering.py`` at post time, not
create time — that way DRAFTs don't burn numbers. ATO requires gap-
free tax-invoice numbering; the counter + row lock in numbering.py
guarantees that.

Posting journal shape (ex-GST line treatment):

    Dr Trade Debtors (AR control) ..... line_total
    Cr Income ......................... line_subtotal (per line)
    Cr GST Collected .................. line_tax (auto-posted by gst.py)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import numbering

_TWOPLACES = Decimal("0.01")


class InvoiceError(ValueError):
    """Raised on invoice validation or state-transition failure."""


# ---------------------------------------------------------------------- #
# Math                                                                    #
# ---------------------------------------------------------------------- #


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal


def _compute_line_totals(
    line: _LineInput, tax_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (subtotal, tax, total) — add-on (ex-GST) tax treatment."""
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor)
    tax = _q2(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax
    return subtotal, tax, total


async def _resolve_tax_rate(
    session: AsyncSession, tax_code_id: uuid.UUID | None
) -> Decimal:
    if tax_code_id is None:
        return Decimal("0")
    tc = await session.get(TaxCode, tax_code_id)
    if tc is None:
        raise InvoiceError(f"Unknown tax code {tax_code_id}")
    return Decimal(str(tc.rate or 0))


# ---------------------------------------------------------------------- #
# CRUD                                                                    #
# ---------------------------------------------------------------------- #


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    issue_date: date,
    due_date: date,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
) -> Invoice:
    inv = Invoice(
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        notes=notes,
        payment_terms=payment_terms,
        status=InvoiceStatus.DRAFT,
    )
    session.add(inv)
    await session.flush()

    if lines:
        await _replace_lines(session, inv, lines)

    await _recalc(session, inv)
    await session.commit()
    return await get(session, inv.id)


async def _replace_lines(
    session: AsyncSession, inv: Invoice, lines: list[dict[str, object]]
) -> None:
    # Hard-delete all existing lines via SQL so the back-populated
    # collection doesn't hold stale rows in the identity map.
    from sqlalchemy import delete as sa_delete
    await session.execute(
        sa_delete(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    )
    await session.flush()
    # Expire the relationship so the next access re-queries.
    session.expire(inv, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=_as_uuid(raw["account_id"]),
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
        )
        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id)
        subtotal, tax, total = _compute_line_totals(line_input, tax_rate)
        session.add(
            InvoiceLine(
                invoice_id=inv.id,
                line_no=i,
                description=line_input.description,
                account_id=line_input.account_id,
                tax_code_id=line_input.tax_code_id,
                quantity=line_input.quantity,
                unit_price=line_input.unit_price,
                discount_pct=line_input.discount_pct,
                line_subtotal=subtotal,
                line_tax=tax,
                line_total=total,
            )
        )
    await session.flush()


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _recalc(session: AsyncSession, inv: Invoice) -> None:
    lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    inv.subtotal = _q2(Decimal(subtotal))
    inv.tax_total = _q2(Decimal(tax))
    inv.total = inv.subtotal + inv.tax_total


async def get(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    result = await session.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(Invoice.id == invoice_id)
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    return inv


async def list_invoices(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: InvoiceStatus | None = None,
    contact_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Invoice]:
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(Invoice.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Invoice.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Invoice.status == status)
    if contact_id is not None:
        stmt = stmt.where(Invoice.contact_id == contact_id)
    stmt = stmt.order_by(Invoice.issue_date.desc(), Invoice.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError(
            f"Cannot edit invoice {inv.id} in state {inv.status.value}; "
            "void the existing invoice and raise a new one instead."
        )
    if contact_id is not None:
        inv.contact_id = contact_id
    if issue_date is not None:
        inv.issue_date = issue_date
    if due_date is not None:
        inv.due_date = due_date
    if notes is not None:
        inv.notes = notes
    if payment_terms is not None:
        inv.payment_terms = payment_terms
    if lines is not None:
        await _replace_lines(session, inv, lines)
    await _recalc(session, inv)
    await session.commit()
    return await get(session, inv.id)


# ---------------------------------------------------------------------- #
# Post / void                                                             #
# ---------------------------------------------------------------------- #


async def _get_ar_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "1-1200",
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise InvoiceError(
            "AR control account 1-1200 Trade Debtors is missing — "
            "re-run the CoA seed."
        )
    return acct


async def post_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status == InvoiceStatus.POSTED:
        raise InvoiceError(f"Invoice {inv.id} is already posted")
    if inv.status == InvoiceStatus.VOIDED:
        raise InvoiceError(f"Invoice {inv.id} is voided; raise a new one")
    if not inv.lines:
        raise InvoiceError("Cannot post an invoice with no lines")
    if inv.total <= Decimal("0"):
        raise InvoiceError(f"Cannot post invoice with non-positive total {inv.total}")

    # Mint the invoice number now (DRAFT never burns a number).
    if not inv.number:
        inv.number = await numbering.next_number(
            session, inv.company_id, "invoice"
        )

    ar_account = await _get_ar_account(session, inv.company_id)

    journal_lines: list[dict[str, object]] = [
        # Line 1: Dr Trade Debtors for the invoice total.
        {
            "account_id": ar_account.id,
            "description": f"Invoice {inv.number}",
            "debit": inv.total,
            "credit": Decimal("0"),
        },
    ]
    # One Cr line per income account per invoice line; GST auto-poster
    # appends the matching Cr GST Collected.
    for line in inv.lines:
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{inv.number}: {line.description}",
                "debit": Decimal("0"),
                "credit": line.line_subtotal,
                "tax_code_id": line.tax_code_id,
                "gst_amount": line.line_tax if line.line_tax > 0 else None,
            }
        )

    entry = await journal_svc.create_draft(
        session,
        company_id=inv.company_id,
        entry_date=inv.issue_date,
        description=f"Invoice {inv.number}",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session, entry.id, posted_by=posted_by, override_reason=override_reason
    )

    inv.status = InvoiceStatus.POSTED
    inv.journal_entry_id = posted.id
    inv.posted_at = datetime.now(UTC)
    inv.posted_by = posted_by
    await session.commit()
    return await get(session, inv.id)


async def void_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status == InvoiceStatus.VOIDED:
        return inv
    if inv.status == InvoiceStatus.DRAFT:
        inv.status = InvoiceStatus.VOIDED
        await session.commit()
        return inv
    if inv.amount_paid > Decimal("0"):
        raise InvoiceError(
            f"Invoice {inv.number} has payments allocated — "
            "unallocate before voiding."
        )
    if inv.journal_entry_id is None:
        raise InvoiceError(f"Posted invoice {inv.id} has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        inv.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void invoice {inv.number}",
    )
    inv.status = InvoiceStatus.VOIDED
    inv.void_journal_entry_id = reversal.id
    await session.commit()
    return inv


async def mark_sent(
    session: AsyncSession, invoice_id: uuid.UUID
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status != InvoiceStatus.POSTED:
        raise InvoiceError("Only POSTED invoices can be marked as sent")
    inv.sent_at = datetime.now(UTC)
    await session.commit()
    return inv


async def archive(
    session: AsyncSession, invoice_id: uuid.UUID
) -> Invoice:
    inv = await get(session, invoice_id)
    inv.archived_at = datetime.now(UTC)
    await session.commit()
    return inv
