"""Payment service — create, allocate, post, void.

Posting rules:

* ``INCOMING`` (customer receipt): Dr Bank, Cr AR control.
* ``OUTGOING`` (supplier payment): Dr AP control, Cr Bank. The
  outgoing flow is exercised once AP / Bills ships in Batch V.

Allocation is decoupled from posting so users can accept receipts on
account (no invoice to match) and allocate later. Allocations update
``Invoice.amount_paid`` — flipping status to POSTED/VOIDED is out of
scope for now (the simplified lifecycle stays DRAFT → POSTED →
VOIDED; amount_paid just carries the remaining balance).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.payment import (
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from saebooks.services import journal as journal_svc
from saebooks.services import numbering


class PaymentError(ValueError):
    """Raised on payment validation or state-transition failure."""


_AR_CODE = "1-1200"
_AP_CODE = "2-1200"


async def _get_control_account(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == code,
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise PaymentError(f"Control account {code} not found — re-run the seed")
    return acct


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    payment_date: date,
    amount: Decimal,
    direction: PaymentDirection = PaymentDirection.INCOMING,
    method: PaymentMethod = PaymentMethod.EFT,
    reference: str | None = None,
    notes: str | None = None,
) -> Payment:
    if amount <= Decimal("0"):
        raise PaymentError("Payment amount must be positive")
    pay = Payment(
        company_id=company_id,
        contact_id=contact_id,
        bank_account_id=bank_account_id,
        payment_date=payment_date,
        amount=amount,
        direction=direction,
        method=method,
        reference=reference,
        notes=notes,
        status=PaymentStatus.DRAFT,
    )
    session.add(pay)
    await session.commit()
    await session.refresh(pay)
    return pay


async def get(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    result = await session.execute(
        select(Payment)
        .options(selectinload(Payment.allocations))
        .where(Payment.id == payment_id)
    )
    pay = result.scalar_one_or_none()
    if pay is None:
        raise PaymentError(f"Payment {payment_id} not found")
    return pay


async def list_payments(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: PaymentStatus | None = None,
    contact_id: uuid.UUID | None = None,
    direction: PaymentDirection | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Payment]:
    stmt = (
        select(Payment)
        .options(selectinload(Payment.allocations))
        .where(Payment.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Payment.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Payment.status == status)
    if contact_id is not None:
        stmt = stmt.where(Payment.contact_id == contact_id)
    if direction is not None:
        stmt = stmt.where(Payment.direction == direction)
    stmt = stmt.order_by(Payment.payment_date.desc(), Payment.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def allocate(
    session: AsyncSession,
    payment_id: uuid.UUID,
    *,
    invoice_allocations: list[tuple[uuid.UUID, Decimal]] | None = None,
    bill_allocations: list[tuple[uuid.UUID, Decimal]] | None = None,
) -> Payment:
    """Attach invoice or bill allocations to a payment. Idempotent-style:
    replaces existing allocations with the given set.

    INCOMING payments expect ``invoice_allocations``; OUTGOING payments
    expect ``bill_allocations``. Mixing the two raises. Each allocation
    is ``(target_id, amount)``; the sum must not exceed the payment
    amount. The target document's ``amount_paid`` is recomputed from
    its full allocation history after this call so repeated allocations
    don't double-count.
    """
    pay = await get(session, payment_id)
    if pay.status == PaymentStatus.VOIDED:
        raise PaymentError("Cannot allocate a voided payment")

    invoice_allocations = invoice_allocations or []
    bill_allocations = bill_allocations or []
    if invoice_allocations and bill_allocations:
        raise PaymentError(
            "A single payment cannot allocate to both invoices and bills"
        )
    if invoice_allocations and pay.direction != PaymentDirection.INCOMING:
        raise PaymentError(
            "Only INCOMING payments may allocate to invoices"
        )
    if bill_allocations and pay.direction != PaymentDirection.OUTGOING:
        raise PaymentError(
            "Only OUTGOING payments may allocate to bills"
        )

    total_requested = sum(
        (a for _, a in invoice_allocations + bill_allocations),
        Decimal("0"),
    )
    if total_requested > pay.amount:
        raise PaymentError(
            f"Total allocated ({total_requested}) exceeds payment amount "
            f"({pay.amount})"
        )
    if any(
        a <= Decimal("0")
        for _, a in invoice_allocations + bill_allocations
    ):
        raise PaymentError("Allocation amounts must be positive")

    # Replace existing allocations wholesale so re-submitting the
    # allocation form from the UI converges on a single set.
    from sqlalchemy import delete as sa_delete
    await session.execute(
        sa_delete(PaymentAllocation).where(
            PaymentAllocation.payment_id == pay.id
        )
    )
    await session.flush()
    session.expire(pay, ["allocations"])

    touched_invoice_ids: set[uuid.UUID] = set()
    touched_bill_ids: set[uuid.UUID] = set()
    for inv_id, amt in invoice_allocations:
        # Verify invoice belongs to this company + is posted.
        inv = await session.get(Invoice, inv_id)
        if inv is None or inv.company_id != pay.company_id:
            raise PaymentError(f"Invoice {inv_id} not found for this company")
        if inv.status != InvoiceStatus.POSTED:
            raise PaymentError(
                f"Invoice {inv.number or inv.id} is not POSTED "
                f"(status={inv.status.value})"
            )
        session.add(
            PaymentAllocation(
                payment_id=pay.id,
                invoice_id=inv_id,
                amount=amt,
            )
        )
        touched_invoice_ids.add(inv_id)
    for bill_id, amt in bill_allocations:
        bill = await session.get(Bill, bill_id)
        if bill is None or bill.company_id != pay.company_id:
            raise PaymentError(f"Bill {bill_id} not found for this company")
        if bill.status != BillStatus.POSTED:
            raise PaymentError(
                f"Bill {bill.number or bill.id} is not POSTED "
                f"(status={bill.status.value})"
            )
        session.add(
            PaymentAllocation(
                payment_id=pay.id,
                bill_id=bill_id,
                amount=amt,
            )
        )
        touched_bill_ids.add(bill_id)
    await session.flush()

    # Recompute amount_paid for each touched target from the FULL
    # allocation history. This gives us the true paid total even if
    # allocations came from multiple payments.
    for inv_id in touched_invoice_ids:
        await _refresh_invoice_amount_paid(session, inv_id)
    for bill_id in touched_bill_ids:
        await _refresh_bill_amount_paid(session, bill_id)

    await session.commit()
    return await get(session, pay.id)


async def _refresh_invoice_amount_paid(
    session: AsyncSession, invoice_id: uuid.UUID
) -> None:
    from sqlalchemy import func
    result = await session.execute(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .join(Payment, PaymentAllocation.payment_id == Payment.id)
        .where(
            PaymentAllocation.invoice_id == invoice_id,
            Payment.status == PaymentStatus.POSTED,
        )
    )
    total = Decimal(str(result.scalar_one() or 0))
    inv = await session.get(Invoice, invoice_id)
    if inv is not None:
        inv.amount_paid = total
    await session.flush()


async def _refresh_bill_amount_paid(
    session: AsyncSession, bill_id: uuid.UUID
) -> None:
    from sqlalchemy import func
    result = await session.execute(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .join(Payment, PaymentAllocation.payment_id == Payment.id)
        .where(
            PaymentAllocation.bill_id == bill_id,
            Payment.status == PaymentStatus.POSTED,
        )
    )
    total = Decimal(str(result.scalar_one() or 0))
    bill = await session.get(Bill, bill_id)
    if bill is not None:
        bill.amount_paid = total
    await session.flush()


async def post_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Payment:
    pay = await get(session, payment_id)
    if pay.status == PaymentStatus.POSTED:
        raise PaymentError(f"Payment {pay.id} is already posted")
    if pay.status == PaymentStatus.VOIDED:
        raise PaymentError(f"Payment {pay.id} is voided")

    if not pay.number:
        pay.number = await numbering.next_number(
            session, pay.company_id, "payment"
        )

    bank_acct = await session.get(Account, pay.bank_account_id)
    if bank_acct is None:
        raise PaymentError("Bank account not found")

    if pay.direction == PaymentDirection.INCOMING:
        control = await _get_control_account(session, pay.company_id, _AR_CODE)
        lines: list[dict[str, object]] = [
            {
                "account_id": bank_acct.id,
                "description": f"Receipt {pay.number}",
                "debit": pay.amount,
                "credit": Decimal("0"),
            },
            {
                "account_id": control.id,
                "description": f"Receipt {pay.number}",
                "debit": Decimal("0"),
                "credit": pay.amount,
            },
        ]
    else:
        control = await _get_control_account(session, pay.company_id, _AP_CODE)
        lines = [
            {
                "account_id": control.id,
                "description": f"Payment {pay.number}",
                "debit": pay.amount,
                "credit": Decimal("0"),
            },
            {
                "account_id": bank_acct.id,
                "description": f"Payment {pay.number}",
                "debit": Decimal("0"),
                "credit": pay.amount,
            },
        ]

    entry = await journal_svc.create_draft(
        session,
        company_id=pay.company_id,
        entry_date=pay.payment_date,
        description=f"{pay.direction.value} payment {pay.number}",
        lines=lines,
    )
    posted = await journal_svc.post(
        session, entry.id, posted_by=posted_by, override_reason=override_reason
    )

    pay.status = PaymentStatus.POSTED
    pay.journal_entry_id = posted.id
    pay.posted_at = datetime.now(UTC)
    pay.posted_by = posted_by
    await session.commit()

    # Refresh allocated targets' amount_paid now that the payment is POSTED.
    invoice_ids = {
        a.invoice_id for a in pay.allocations if a.invoice_id is not None
    }
    bill_ids = {a.bill_id for a in pay.allocations if a.bill_id is not None}
    for inv_id in invoice_ids:
        await _refresh_invoice_amount_paid(session, inv_id)
    for bill_id in bill_ids:
        await _refresh_bill_amount_paid(session, bill_id)
    if invoice_ids or bill_ids:
        await session.commit()

    return await get(session, pay.id)


async def void_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Payment:
    pay = await get(session, payment_id)
    if pay.status == PaymentStatus.VOIDED:
        return pay
    if pay.status == PaymentStatus.DRAFT:
        pay.status = PaymentStatus.VOIDED
        await session.commit()
        return pay
    if pay.journal_entry_id is None:
        raise PaymentError("Posted payment has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        pay.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void payment {pay.number}",
    )
    pay.status = PaymentStatus.VOIDED
    pay.void_journal_entry_id = reversal.id
    await session.commit()

    # Zero allocations against the voided payment so downstream
    # ``amount_paid`` recalc treats it as un-allocated.
    invoice_ids = {
        a.invoice_id for a in pay.allocations if a.invoice_id is not None
    }
    bill_ids = {a.bill_id for a in pay.allocations if a.bill_id is not None}
    for inv_id in invoice_ids:
        await _refresh_invoice_amount_paid(session, inv_id)
    for bill_id in bill_ids:
        await _refresh_bill_amount_paid(session, bill_id)
    if invoice_ids or bill_ids:
        await session.commit()

    return pay


async def archive(
    session: AsyncSession, payment_id: uuid.UUID
) -> Payment:
    pay = await get(session, payment_id)
    pay.archived_at = datetime.now(UTC)
    await session.commit()
    return pay


# Re-export the enums for convenience so callers can do
# ``from saebooks.services.payments import PaymentDirection``
__all__ = [
    "CreditNote",
    "CreditNoteStatus",
    "PaymentDirection",
    "PaymentError",
    "PaymentMethod",
    "PaymentStatus",
    "allocate",
    "archive",
    "create_draft",
    "get",
    "list_payments",
    "post_payment",
    "void_payment",
]
