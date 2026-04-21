"""AP bill service — create, update, post, void, archive.

Mirror of ``services/invoices.py``. All GL-impacting operations go
through ``services/journal.py`` — bills never touch
``journal_entries`` directly. GST auto-posting is wired up in
``gst.py``: a line with ``tax_code_id`` + ``gst_amount`` on an EXPENSE
account gets a matching DR GST Paid appended during post.

Posting journal shape (ex-GST line treatment):

    Dr Expense (per line) ......... line_subtotal
    Dr GST Paid ................... line_tax (auto-posted by gst.py)
    Cr Trade Creditors (AP) ....... total
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillLine, BillStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import numbering

_TWOPLACES = Decimal("0.01")


class BillError(ValueError):
    """Raised on bill validation or state-transition failure."""


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
    project_id: uuid.UUID | None


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
        raise BillError(f"Unknown tax code {tax_code_id}")
    return Decimal(str(tc.rate or 0))


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


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
    supplier_reference: str | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
) -> Bill:
    bill = Bill(
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        supplier_reference=supplier_reference,
        notes=notes,
        status=BillStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
    )
    session.add(bill)
    await session.flush()

    if lines:
        await _replace_lines(session, bill, lines)

    await _recalc(session, bill)
    await session.commit()
    return await get(session, bill.id)


async def _replace_lines(
    session: AsyncSession, bill: Bill, lines: list[dict[str, object]]
) -> None:
    # Hard-delete existing lines via SQL so the identity map doesn't
    # carry stale rows — same pattern as invoices.
    await session.execute(
        sa_delete(BillLine).where(BillLine.bill_id == bill.id)
    )
    await session.flush()
    session.expire(bill, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        project_id = raw.get("project_id")
        if isinstance(project_id, str) and project_id:
            project_id = uuid.UUID(project_id)
        elif not project_id:
            project_id = None

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=_as_uuid(raw["account_id"]),
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
            project_id=project_id if isinstance(project_id, uuid.UUID) else None,
        )
        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id)
        subtotal, tax, total = _compute_line_totals(line_input, tax_rate)
        session.add(
            BillLine(
                bill_id=bill.id,
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
                project_id=line_input.project_id,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, bill: Bill) -> None:
    lines = (
        await session.execute(
            select(BillLine).where(BillLine.bill_id == bill.id)
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    bill.subtotal = _q2(Decimal(subtotal))
    bill.tax_total = _q2(Decimal(tax))
    bill.total = bill.subtotal + bill.tax_total

    # Foreign-currency shadow totals. Same pattern as invoices — sum
    # per-line base contributions so header base_total matches the sum
    # of per-line journal lines that post_bill will emit.
    rate = Decimal(str(bill.fx_rate or Decimal("1")))
    base_subtotal = sum(
        (_q2(ln.line_subtotal * rate) for ln in lines), Decimal("0")
    )
    base_tax = sum((_q2(ln.line_tax * rate) for ln in lines), Decimal("0"))
    bill.base_subtotal = _q2(Decimal(base_subtotal))
    bill.base_tax_total = _q2(Decimal(base_tax))
    bill.base_total = bill.base_subtotal + bill.base_tax_total
    bill.base_amount_paid = _q2(Decimal(bill.amount_paid) * rate)


async def get(session: AsyncSession, bill_id: uuid.UUID) -> Bill:
    result = await session.execute(
        select(Bill)
        .options(selectinload(Bill.lines))
        .where(Bill.id == bill_id)
    )
    bill = result.scalar_one_or_none()
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    return bill


async def list_bills(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: BillStatus | None = None,
    contact_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Bill]:
    stmt = (
        select(Bill)
        .options(selectinload(Bill.lines))
        .where(Bill.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Bill.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Bill.status == status)
    if contact_id is not None:
        stmt = stmt.where(Bill.contact_id == contact_id)
    stmt = stmt.order_by(Bill.issue_date.desc(), Bill.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    supplier_reference: str | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
) -> Bill:
    bill = await get(session, bill_id)
    if bill.status != BillStatus.DRAFT:
        raise BillError(
            f"Cannot edit bill {bill.id} in state {bill.status.value}; "
            "void the existing bill and raise a new one instead."
        )
    if contact_id is not None:
        bill.contact_id = contact_id
    if issue_date is not None:
        bill.issue_date = issue_date
    if due_date is not None:
        bill.due_date = due_date
    if supplier_reference is not None:
        bill.supplier_reference = supplier_reference
    if notes is not None:
        bill.notes = notes
    if currency is not None:
        bill.currency = currency.upper()
    if fx_rate is not None:
        bill.fx_rate = fx_rate
    if lines is not None:
        await _replace_lines(session, bill, lines)
    await _recalc(session, bill)
    await session.commit()
    return await get(session, bill.id)


# ---------------------------------------------------------------------- #
# Post / void                                                             #
# ---------------------------------------------------------------------- #


async def _get_ap_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "2-1200",
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise BillError(
            "AP control account 2-1200 Trade Creditors is missing — "
            "re-run the CoA seed."
        )
    return acct


async def post_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Bill:
    bill = await get(session, bill_id)
    if bill.status == BillStatus.POSTED:
        raise BillError(f"Bill {bill.id} is already posted")
    if bill.status == BillStatus.VOIDED:
        raise BillError(f"Bill {bill.id} is voided; raise a new one")
    if not bill.lines:
        raise BillError("Cannot post a bill with no lines")
    if bill.total <= Decimal("0"):
        raise BillError(f"Cannot post bill with non-positive total {bill.total}")

    # Mint the internal bill number now (DRAFT never burns a number).
    if not bill.number:
        bill.number = await numbering.next_number(
            session, bill.company_id, "bill"
        )

    ap_account = await _get_ap_account(session, bill.company_id)
    ref = bill.supplier_reference or bill.number

    # Post the journal in base currency. AUD-only: rate=1, base_*=
    # unscaled, behaviour unchanged. Foreign-currency: per-line
    # Dr + GST are translated at the bill's rate.
    rate = Decimal(str(bill.fx_rate or Decimal("1")))

    journal_lines: list[dict[str, object]] = []
    # One Dr line per expense/asset account per bill line; GST
    # auto-poster appends the matching Dr GST Paid. project_id rides
    # through so P&L-by-project can pick up cost-side postings.
    for line in bill.lines:
        line_base_subtotal = _q2(line.line_subtotal * rate)
        line_base_tax = (
            _q2(line.line_tax * rate) if line.line_tax > 0 else None
        )
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{bill.number}: {line.description}",
                "debit": line_base_subtotal,
                "credit": Decimal("0"),
                "tax_code_id": line.tax_code_id,
                "gst_amount": line_base_tax,
                "project_id": line.project_id,
            }
        )
    # Cr Trade Creditors for the base-currency total.
    journal_lines.append(
        {
            "account_id": ap_account.id,
            "description": f"Bill {bill.number} ({ref})",
            "debit": Decimal("0"),
            "credit": bill.base_total,
        }
    )

    entry = await journal_svc.create_draft(
        session,
        company_id=bill.company_id,
        entry_date=bill.issue_date,
        description=f"Bill {bill.number} ({ref})",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session, entry.id, posted_by=posted_by, override_reason=override_reason
    )

    bill.status = BillStatus.POSTED
    bill.journal_entry_id = posted.id
    bill.posted_at = datetime.now(UTC)
    bill.posted_by = posted_by
    await session.commit()
    return await get(session, bill.id)


async def void_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Bill:
    bill = await get(session, bill_id)
    if bill.status == BillStatus.VOIDED:
        return bill
    if bill.status == BillStatus.DRAFT:
        bill.status = BillStatus.VOIDED
        await session.commit()
        return bill
    if bill.amount_paid > Decimal("0"):
        raise BillError(
            f"Bill {bill.number} has payments allocated — "
            "unallocate before voiding."
        )
    if bill.journal_entry_id is None:
        raise BillError(f"Posted bill {bill.id} has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        bill.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void bill {bill.number}",
    )
    bill.status = BillStatus.VOIDED
    bill.void_journal_entry_id = reversal.id
    await session.commit()
    return bill


async def archive(
    session: AsyncSession, bill_id: uuid.UUID
) -> Bill:
    bill = await get(session, bill_id)
    bill.archived_at = datetime.now(UTC)
    await session.commit()
    return bill
