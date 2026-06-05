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

Foreign currency (Batch GG/2):
    Payments carry their own ``currency`` + ``fx_rate`` which may
    differ from the invoice / bill rate. At post time we walk the
    allocations, translate each allocation to base at the *invoice /
    bill* rate (the rate that originally stamped AR / AP), and plug
    the difference vs ``pay.base_amount`` into ``6-1630 Exchange Rate
    Loss`` / ``6-1640 Exchange Rate Gain``. In a single-currency (AUD)
    install all rates collapse to 1 and behaviour is identical to the
    pre-FX shape.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import JournalOrigin
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
_FX_GAIN_CODE = "6-1640"  # Exchange Rate Gain (INCOME per seed)
_FX_LOSS_CODE = "6-1630"  # Exchange Rate Loss (EXPENSE per seed)
_TWOPLACES = Decimal("0.01")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


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


async def _validate_party_company_and_tenant(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    bank_account_id: uuid.UUID,
) -> None:
    """Reject payment input whose ``contact_id`` or ``bank_account_id``
    belongs to a different company (Layer-2 cross-company isolation).

    Round-2 critic 09 caught the equivalent gap on /contacts/{id} READ
    (fixed in 26f99fc); Round-3 probe surfaced it on POST /payments,
    where the X-Company-Id was honoured but the FK targets were not.
    Without this guard a tenant can post a payment whose bank account
    or contact belongs to a sibling company — silent cross-company
    write.
    """
    if contact_id is not None:
        c_row = (
            await session.execute(
                select(Contact.id, Contact.company_id, Contact.tenant_id)
                .where(Contact.id == contact_id)
            )
        ).first()
        if (
            c_row is None
            or c_row.company_id != company_id
            or c_row.tenant_id != tenant_id
        ):
            raise PaymentError(
                f"contact {contact_id} does not belong to this company"
            )

    b_row = (
        await session.execute(
            select(Account.id, Account.company_id, Account.tenant_id)
            .where(Account.id == bank_account_id)
        )
    ).first()
    if (
        b_row is None
        or b_row.company_id != company_id
        or b_row.tenant_id != tenant_id
    ):
        raise PaymentError(
            f"bank_account {bank_account_id} does not belong to this company"
        )


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
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
) -> Payment:
    if amount <= Decimal("0"):
        raise PaymentError("Payment amount must be positive")
    rate = fx_rate if fx_rate is not None else Decimal("1")
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
        currency=currency.upper(),
        fx_rate=rate,
        base_amount=_q2(amount * rate),
    )
    session.add(pay)
    await session.commit()
    await session.refresh(pay)
    return pay


async def get(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    result = await session.execute(
        select(Payment)
        .options(
            selectinload(Payment.allocations),
            selectinload(Payment.one_off_vendor),
            selectinload(Payment.one_off_customer),
        )
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
        .options(
            selectinload(Payment.allocations),
            selectinload(Payment.one_off_vendor),
            selectinload(Payment.one_off_customer),
        )
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
        # Currency guard: cross-currency allocation is out of scope for
        # v1. Same-currency pay ↔ inv at different rates is fine — that's
        # the realised-FX path handled in post_payment.
        if inv.currency != pay.currency:
            raise PaymentError(
                f"Invoice {inv.number or inv.id} is in {inv.currency}; "
                f"payment is in {pay.currency}. Cross-currency settlement "
                "is not supported in v1."
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
        if bill.currency != pay.currency:
            raise PaymentError(
                f"Bill {bill.number or bill.id} is in {bill.currency}; "
                f"payment is in {pay.currency}. Cross-currency settlement "
                "is not supported in v1."
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
        # base_amount_paid tracks the amount paid translated at the
        # invoice's own rate — that's the portion of AR that actually
        # cleared. Realised FX lives in the GL on the payment's
        # journal, not here.
        inv.base_amount_paid = _q2(total * Decimal(str(inv.fx_rate)))
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
        bill.base_amount_paid = _q2(total * Decimal(str(bill.fx_rate)))
    await session.flush()


async def _compute_control_credit(
    session: AsyncSession,
    pay: Payment,
) -> tuple[dict[str, Decimal], Decimal]:
    """Compute per-control-account base-currency amounts for a payment.

    The control account is chosen from the **allocation target**, not
    the payment direction alone. This is Round-2 audit fix #11 — the
    previous version routed every OUTGOING payment to AP regardless
    of target, which posts a customer refund (OUTGOING allocated to
    a credit_note) as Dr AP / Cr Bank instead of Dr AR / Cr Bank,
    leaving a dangling credit in AR and a phantom liability in AP.

    Routing table:

    * allocation → invoice     → AR control (1-1200)
    * allocation → bill        → AP control (2-1200)
    * allocation → credit_note → AR control (1-1200) — credit notes
      are customer-side only in this codebase, so settling one (cash
      refund: OUTGOING; on-account: INCOMING is application-only and
      doesn't reach a payment row) reduces AR.

    Returns ``({control_code: base_amount}, allocated_doc_amount)``
    where each ``base_amount`` is the base-currency value to put on the
    control side of the journal against that control account. Each
    allocation is converted at the *document's* fx_rate so AR/AP
    clear at the rate that stamped them.

    Any unallocated remainder is added to the "default" control for
    the payment direction (AR for INCOMING, AP for OUTGOING) at the
    payment's own rate — that's the "on account" balance.

    Caller derives unallocated = ``pay.amount - allocated_doc_amount``.
    """
    allocated_doc = Decimal("0")
    per_control: dict[str, Decimal] = {}

    for a in pay.allocations:
        amt = Decimal(str(a.amount))
        if a.invoice_id is not None:
            inv = await session.get(Invoice, a.invoice_id)
            if inv is None:
                continue
            allocated_doc += amt
            base = _q2(amt * Decimal(str(inv.fx_rate)))
            per_control[_AR_CODE] = per_control.get(_AR_CODE, Decimal("0")) + base
        elif a.bill_id is not None:
            bill = await session.get(Bill, a.bill_id)
            if bill is None:
                continue
            allocated_doc += amt
            base = _q2(amt * Decimal(str(bill.fx_rate)))
            per_control[_AP_CODE] = per_control.get(_AP_CODE, Decimal("0")) + base
        elif a.credit_note_id is not None:
            # Credit notes are customer-side (AR). They have no fx_rate
            # of their own (AUD-only) — translate at the payment's rate.
            cn = await session.get(CreditNote, a.credit_note_id)
            if cn is None:
                continue
            allocated_doc += amt
            base = _q2(amt * Decimal(str(pay.fx_rate)))
            per_control[_AR_CODE] = per_control.get(_AR_CODE, Decimal("0")) + base

    # Unallocated remainder lands on the default control for the
    # direction (AR for INCOMING, AP for OUTGOING) at the payment's
    # own rate — this is the "on account" balance.
    unallocated_doc = Decimal(str(pay.amount)) - allocated_doc
    if unallocated_doc != Decimal("0"):
        default_code = (
            _AR_CODE
            if pay.direction == PaymentDirection.INCOMING
            else _AP_CODE
        )
        base = _q2(unallocated_doc * Decimal(str(pay.fx_rate)))
        per_control[default_code] = (
            per_control.get(default_code, Decimal("0")) + base
        )

    # Quantize each total and drop zero rows.
    per_control = {k: _q2(v) for k, v in per_control.items() if v != Decimal("0")}
    return per_control, allocated_doc


async def _get_fx_accounts(
    session: AsyncSession, company_id: uuid.UUID
) -> tuple[Account | None, Account | None]:
    """Return (gain_account, loss_account). Either may be ``None`` on a
    site with a trimmed-down CoA — the caller then skips FX posting
    with a warning rather than crashing.
    """
    gain = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == _FX_GAIN_CODE,
            Account.archived_at.is_(None),
        )
    )
    loss = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == _FX_LOSS_CODE,
            Account.archived_at.is_(None),
        )
    )
    return gain.scalar_one_or_none(), loss.scalar_one_or_none()



async def _build_cashbook_receipt_lines(
    session: AsyncSession,
    pay: Payment,
    bank_acct: Account,
) -> list[dict[str, object]]:
    """For INCOMING payments in cashbook mode: build per-invoice-line
    Cr Income (with GST auto-poster) lines. No A/R hop.

    Each allocation is split across its invoice's lines proportional to
    each line's ex-GST subtotal. The GST auto-poster in
    ``services/journal.py`` will append the matching Cr GST Collected
    when ``gst_amount`` + ``tax_code_id`` are present on an INCOME line.

    Cashbook mode is AUD-only — invoice/payment fx_rate is treated as 1.
    Unallocated remainder is refused (caller must fully allocate in
    cashbook mode — no "on account" receipts).
    """
    lines: list[dict[str, object]] = [
        {
            "account_id": bank_acct.id,
            "description": f"Receipt {pay.number}",
            "debit": Decimal(str(pay.base_amount or pay.amount)),
            "credit": Decimal("0"),
        },
    ]

    allocated = Decimal("0")
    for a in pay.allocations:
        if a.invoice_id is None:
            raise PaymentError(
                "Cashbook-mode receipts must allocate to an invoice — "
                "on-account receipts are not supported. Upgrade to full "
                "mode to accept on-account."
            )
        inv = (
            await session.execute(
                select(Invoice)
                .options(selectinload(Invoice.lines))
                .where(Invoice.id == a.invoice_id)
            )
        ).scalar_one_or_none()
        if inv is None:
            continue
        allocated += Decimal(str(a.amount))
        if inv.total == Decimal("0"):
            continue
        ratio = Decimal(str(a.amount)) / Decimal(str(inv.total))
        for ln in inv.lines:
            line_subtotal = Decimal(str(ln.line_subtotal or 0))
            line_tax = Decimal(str(ln.line_tax or 0))
            cr_amount = _q2(line_subtotal * ratio)
            cr_tax = _q2(line_tax * ratio) if line_tax > 0 else None
            if cr_amount <= Decimal("0"):
                continue
            lines.append(
                {
                    "account_id": ln.account_id,
                    "description": f"{inv.number}: {ln.description}",
                    "debit": Decimal("0"),
                    "credit": cr_amount,
                    "tax_code_id": ln.tax_code_id,
                    "gst_amount": cr_tax,
                    "project_id": ln.project_id,
                }
            )

    if allocated != Decimal(str(pay.amount)):
        raise PaymentError(
            "Cashbook-mode receipts must be fully allocated to invoices. "
            f"Allocated {allocated} of {pay.amount}."
        )
    return lines


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

    # Base-currency totals. For AUD-only installs base_amount == amount
    # and the control credit equals it, so the resulting journal is
    # identical to the pre-FX shape.
    bank_base = Decimal(str(pay.base_amount or pay.amount))
    per_control, _alloc_doc = await _compute_control_credit(session, pay)
    control_base_total = sum(per_control.values(), Decimal("0"))
    fx_delta = bank_base - control_base_total  # Dr - Cr (INCOMING perspective)

    if pay.direction == PaymentDirection.INCOMING:
        from saebooks.services import edition as edition_svc
        if await edition_svc.is_cashbook_mode(session, pay.company_id):
            # Single-entry receipt: Dr Bank / Cr Income (+ GST auto).
            # No A/R hop, no FX (cashbook is AUD-only).
            lines = await _build_cashbook_receipt_lines(
                session, pay, bank_acct
            )
            fx_delta = Decimal("0")
        else:
            lines: list[dict[str, object]] = [
                {
                    "account_id": bank_acct.id,
                    "description": f"Receipt {pay.number}",
                    "debit": bank_base,
                    "credit": Decimal("0"),
                },
            ]
            # One Cr line per control account touched. For a pure
            # invoice-allocated receipt this is exactly one AR line at
                # the same total as before — identical to the pre-fix shape.
            for code, base in per_control.items():
                ctrl = await _get_control_account(session, pay.company_id, code)
                lines.append(
                    {
                        "account_id": ctrl.id,
                        "description": f"Receipt {pay.number}",
                        "debit": Decimal("0"),
                        "credit": base,
                    }
                )
    else:
        # OUTGOING: Cr Bank for the cash paid out + one Dr line per
        # control account touched (AP for bills, AR for credit-note
        # refunds — see _compute_control_credit for the routing).
        lines = [
            {
                "account_id": bank_acct.id,
                "description": f"Payment {pay.number}",
                "debit": Decimal("0"),
                "credit": bank_base,
            },
        ]
        for code, base in per_control.items():
            ctrl = await _get_control_account(session, pay.company_id, code)
            lines.insert(
                0,
                {
                    "account_id": ctrl.id,
                    "description": f"Payment {pay.number}",
                    "debit": base,
                    "credit": Decimal("0"),
                },
            )
        # For OUTGOING: Dr control_total, Cr bank_base. Delta from
        # "control > bank" means AP cleared more than bank paid —
        # we owed more than we actually paid → GAIN. Flip the sign so
        # the rest of the function uses a single "positive = gain" rule.
        fx_delta = control_base_total - bank_base

    # Post the realised FX gain / loss plug. Sign convention: positive
    # delta = gain (Cr Exchange Rate Gain), negative = loss (Dr
    # Exchange Rate Loss). In AUD-only installs ``fx_delta == 0`` and
    # this block is a no-op.
    if fx_delta != Decimal("0"):
        gain_acct, loss_acct = await _get_fx_accounts(session, pay.company_id)
        if fx_delta > Decimal("0") and gain_acct is not None:
            lines.append(
                {
                    "account_id": gain_acct.id,
                    "description": f"Realised FX gain on {pay.number}",
                    "debit": Decimal("0"),
                    "credit": fx_delta,
                }
            )
        elif fx_delta < Decimal("0") and loss_acct is not None:
            lines.append(
                {
                    "account_id": loss_acct.id,
                    "description": f"Realised FX loss on {pay.number}",
                    "debit": -fx_delta,
                    "credit": Decimal("0"),
                }
            )
        else:
            # FX accounts missing from the CoA — refuse to post a
            # silently-unbalanced journal. Caller must seed 6-1630 /
            # 6-1640 or settle at the invoice rate.
            raise PaymentError(
                "Realised FX gain/loss detected but Exchange Rate Gain / "
                "Loss accounts are missing — re-run the AU CoA seed."
            )

    entry = await journal_svc.create_draft(
        session,
        company_id=pay.company_id,
        tenant_id=pay.tenant_id,
        entry_date=pay.payment_date,
        description=f"{pay.direction.value} payment {pay.number}",
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.PAYMENT,
        source_type="payment",
        source_id=pay.id,
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
        tenant_id=pay.tenant_id,
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
    "VersionConflict",
    "allocate",
    "api_create",
    "api_get",
    "api_post_payment",
    "api_update",
    "api_void",
    "archive",
    "create_draft",
    "get",
    "list_active",
    "list_payments",
    "post_payment",
    "void_payment",
]


# ==========================================================================
# API-oriented service (cycle 9) — optimistic locking + change_log
#
# These functions are the API surface for /api/v1/payments.  They are
# intentionally separate from the legacy posting pipeline above so the
# two surfaces can evolve independently.
# ==========================================================================

from sqlalchemy import func  # noqa: E402

from saebooks.services import audit_log as audit_log_svc  # noqa: E402
from saebooks.services import change_log as change_log_svc  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Payment) -> None:
        super().__init__(
            f"Payment {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Columns serialised into change_log.payload
# ---------------------------------------------------------------------------

_PAYMENT_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "bank_account_id",
    "number",
    "direction",
    "method",
    "status",
    "payment_date",
    "amount",
    "currency",
    "fx_rate",
    "base_amount",
    "reference",
    "notes",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise_payment(pay: Payment) -> dict:
    from decimal import Decimal as _D

    data: dict = {}
    for key in _PAYMENT_COLUMNS:
        val = getattr(pay, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, _D):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _validate_alloc_target(
    alloc: dict, *, direction: PaymentDirection
) -> None:
    """Enforce XOR + direction constraints on an allocation dict pre-DB.

    Round-2 audit fix #12: critic 18 reported supplier bill payments
    crashing because the allocation row landed with the bill UUID in
    ``invoice_id`` instead of ``bill_id``. The XOR DB constraint
    catches the bad row but the request 500s instead of returning a
    clean 422. This validator runs in the service layer before the
    INSERT so a caller passing both keys (or none) gets a clear error.

    Rules:

    * Exactly one of ``invoice_id`` / ``bill_id`` / ``credit_note_id``
      must be set on each allocation.
    * INCOMING payments may not allocate to bills (allocate to invoices
      or, as a future-proof, vendor credit_notes).
    * OUTGOING payments may not allocate to invoices (allocate to
      bills, or to credit_notes for customer refunds).

    Mirrors the legacy ``svc.allocate()`` direction guard so the
    JSON-API and the form-handler agree.
    """
    invoice_id = alloc.get("invoice_id")
    bill_id = alloc.get("bill_id")
    credit_note_id = alloc.get("credit_note_id")
    n_set = sum(1 for v in (invoice_id, bill_id, credit_note_id) if v)
    if n_set == 0:
        raise PaymentError(
            "Allocation must target exactly one of invoice_id, bill_id, "
            "or credit_note_id"
        )
    if n_set > 1:
        keys = [
            k for k, v in (
                ("invoice_id", invoice_id),
                ("bill_id", bill_id),
                ("credit_note_id", credit_note_id),
            )
            if v
        ]
        raise PaymentError(
            f"Allocation may target only one document — got {keys}. "
            "Pre-emptive guard for the XOR CHECK on payment_allocations."
        )
    if direction == PaymentDirection.INCOMING and bill_id:
        raise PaymentError(
            f"INCOMING payment cannot allocate to bill {bill_id}. "
            "Did you mean credit_note_id (vendor credit) or invoice_id?"
        )
    if direction == PaymentDirection.OUTGOING and invoice_id:
        raise PaymentError(
            f"OUTGOING payment cannot allocate to invoice {invoice_id}. "
            "Did you mean bill_id (supplier bill) or credit_note_id "
            "(customer refund)?"
        )


async def _get_with_allocations(
    session: AsyncSession, payment_id: uuid.UUID
) -> Payment | None:
    result = await session.execute(
        select(Payment)
        .options(
            selectinload(Payment.allocations),
            selectinload(Payment.one_off_vendor),
            selectinload(Payment.one_off_customer),
        )
        .where(Payment.id == payment_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    direction: PaymentDirection | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Payment], int]:
    """Return (payments, total_count) — excludes archived payments."""
    base_where = [
        Payment.company_id == company_id,
        Payment.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(Payment.contact_id == contact_id)
    if direction is not None:
        base_where.append(Payment.direction == direction)
    if date_from is not None:
        base_where.append(Payment.payment_date >= date_from)
    if date_to is not None:
        base_where.append(Payment.payment_date <= date_to)

    count_stmt = select(func.count()).select_from(Payment).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Payment)
        .options(
            selectinload(Payment.allocations),
            selectinload(Payment.one_off_vendor),
            selectinload(Payment.one_off_customer),
        )
        .where(*base_where)
        .order_by(Payment.payment_date.desc(), Payment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    payments = list((await session.execute(stmt)).scalars().unique().all())
    return payments, total


async def api_get(
    session: AsyncSession,
    payment_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Payment | None:
    """Fetch a single payment with its allocations. Returns None if not found.

    P0 cross-tenant leak fix: when ``tenant_id`` is supplied, the
    lookup is filtered by tenant — a foreign-tenant id returns
    ``None`` even if the row exists. The parameter is keyword-only
    and optional so existing callers keep working unchanged; the
    API layer always supplies it.
    """
    if tenant_id is None and company_id is None:
        return await _get_with_allocations(session, payment_id)
    clauses = [Payment.id == payment_id]
    if tenant_id is not None:
        clauses.append(Payment.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Payment.company_id == company_id)
    result = await session.execute(
        select(Payment)
        .options(
            selectinload(Payment.allocations),
            selectinload(Payment.one_off_vendor),
            selectinload(Payment.one_off_customer),
        )
        .where(*clauses)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    contact_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    payment_date: date,
    amount: Decimal,
    direction: PaymentDirection = PaymentDirection.INCOMING,
    method: PaymentMethod = PaymentMethod.EFT,
    reference: str | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    allocations: list[dict] | None = None,
) -> Payment:
    """Create a payment draft with version=1 and a change_log row."""
    if amount <= Decimal("0"):
        raise PaymentError("Payment amount must be positive")
    # Layer-2 cross-company isolation — Round-3 probe finding (mirrors
    # Round-2 critic 09 fix on /contacts/{id}). X-Company-Id has already
    # narrowed the caller's scope to ``company_id``; this guard rejects
    # FK injection of a contact or bank account from a sibling company.
    await _validate_party_company_and_tenant(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        bank_account_id=bank_account_id,
    )
    rate = fx_rate if fx_rate is not None else Decimal("1")
    pay = Payment(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        bank_account_id=bank_account_id,
        payment_date=payment_date,
        amount=amount,
        direction=direction,
        method=method,
        reference=reference,
        notes=notes,
        status=PaymentStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=rate,
        base_amount=_q2(amount * rate),
        version=1,
    )
    session.add(pay)
    await session.flush()
    await session.refresh(pay)

    # Attach allocations if provided
    if allocations:
        for alloc in allocations:
            _validate_alloc_target(alloc, direction=direction)
            invoice_id = alloc.get("invoice_id")
            bill_id = alloc.get("bill_id")
            credit_note_id = alloc.get("credit_note_id")
            alloc_amount = Decimal(str(alloc["amount"]))
            if alloc_amount <= Decimal("0"):
                raise PaymentError("Allocation amounts must be positive")
            session.add(
                PaymentAllocation(
                    payment_id=pay.id,
                    invoice_id=uuid.UUID(str(invoice_id)) if invoice_id else None,
                    bill_id=uuid.UUID(str(bill_id)) if bill_id else None,
                    credit_note_id=uuid.UUID(str(credit_note_id)) if credit_note_id else None,
                    amount=alloc_amount,
                )
            )
        await session.flush()

    pay_loaded = await _get_with_allocations(session, pay.id)
    assert pay_loaded is not None

    await change_log_svc.append(
        session,
        entity="payment",
        entity_id=pay_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise_payment(pay_loaded),
        version=pay_loaded.version,
    )
    await session.commit()
    return await _get_with_allocations(session, pay_loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    payment_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    contact_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    payment_date: date | None = None,
    amount: Decimal | None = None,
    direction: PaymentDirection | None = None,
    method: PaymentMethod | None = None,
    reference: str | None = None,
    notes: str | None = None,
    currency: str | None = None,
    allocations: list[dict] | None = None,
) -> Payment:
    """Update a payment draft with optimistic locking + change_log."""
    pay = await _get_with_allocations(session, payment_id)
    if pay is None:
        raise PaymentError(f"Payment {payment_id} not found")
    if pay.version != expected_version:
        raise VersionConflict(pay)

    if contact_id is not None:
        pay.contact_id = contact_id
    if bank_account_id is not None:
        pay.bank_account_id = bank_account_id
    if payment_date is not None:
        pay.payment_date = payment_date
    if amount is not None:
        if amount <= Decimal("0"):
            raise PaymentError("Payment amount must be positive")
        pay.amount = amount
        pay.base_amount = _q2(amount * Decimal(str(pay.fx_rate)))
    if direction is not None:
        pay.direction = direction
    if method is not None:
        pay.method = method
    if reference is not None:
        pay.reference = reference
    if notes is not None:
        pay.notes = notes
    if currency is not None:
        pay.currency = currency.upper()
    if allocations is not None:
        from sqlalchemy import delete as sa_delete
        await session.execute(
            sa_delete(PaymentAllocation).where(
                PaymentAllocation.payment_id == pay.id
            )
        )
        await session.flush()
        for alloc in allocations:
            _validate_alloc_target(alloc, direction=pay.direction)
            invoice_id = alloc.get("invoice_id")
            bill_id = alloc.get("bill_id")
            credit_note_id = alloc.get("credit_note_id")
            alloc_amount = Decimal(str(alloc["amount"]))
            if alloc_amount <= Decimal("0"):
                raise PaymentError("Allocation amounts must be positive")
            session.add(
                PaymentAllocation(
                    payment_id=pay.id,
                    invoice_id=uuid.UUID(str(invoice_id)) if invoice_id else None,
                    bill_id=uuid.UUID(str(bill_id)) if bill_id else None,
                    credit_note_id=uuid.UUID(str(credit_note_id)) if credit_note_id else None,
                    amount=alloc_amount,
                )
            )

    pay.version = pay.version + 1
    await session.flush()
    await session.refresh(pay)

    pay_loaded = await _get_with_allocations(session, payment_id)
    assert pay_loaded is not None

    await change_log_svc.append(
        session,
        entity="payment",
        entity_id=pay_loaded.id,
        op="update",
        actor=actor,
        payload=_serialise_payment(pay_loaded),
        version=pay_loaded.version,
    )
    await session.commit()
    return await _get_with_allocations(session, payment_id)  # type: ignore[return-value]


async def api_void(
    session: AsyncSession,
    payment_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> Payment:
    """Soft-delete (archive/void) a payment with optimistic locking + change_log."""
    pay = await _get_with_allocations(session, payment_id)
    if pay is None:
        raise PaymentError(f"Payment {payment_id} not found")
    if pay.version != expected_version:
        raise VersionConflict(pay)

    pay.archived_at = datetime.now(UTC)
    pay.status = PaymentStatus.VOIDED
    pay.version = pay.version + 1
    await session.flush()
    await session.refresh(pay)

    pay_loaded = await _get_with_allocations(session, payment_id)
    assert pay_loaded is not None

    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=pay_loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.PAYMENT_VOID,
            table_name="payments",
            row_id=str(pay_loaded.id),
            row_snapshot=_serialise_payment(pay_loaded),
            reason=f"API void by {actor}",
        )
    await change_log_svc.append(
        session,
        entity="payment",
        entity_id=pay_loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise_payment(pay_loaded),
        version=pay_loaded.version,
    )
    await session.commit()
    return await _get_with_allocations(session, payment_id)  # type: ignore[return-value]


async def api_post_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Payment:
    """Transition DRAFT → POSTED with optimistic locking + change_log.

    Wraps the legacy post_payment() pipeline, adding:
    - tenant isolation guard
    - optimistic-locking check (VersionConflict on mismatch)
    - payment_not_draft gate (PaymentError on non-DRAFT)
    - version bump + change_log row after posting succeeds

    Raises:
        PaymentError: payment not found, or already POSTED/VOIDED
        VersionConflict: expected_version does not match stored value
        PostingError (from journal layer): cross-company account reference
            or other journal integrity violation
    """
    from saebooks.services.journal import (
        PostingError as _PostingError,  # noqa: F401 (re-exported via raise)
    )

    pay = await _get_with_allocations(session, payment_id)
    if pay is None:
        raise PaymentError(f"Payment {payment_id} not found")
    if tenant_id is not None and pay.tenant_id != tenant_id:
        raise PaymentError(f"Payment {payment_id} not found")
    if pay.version != expected_version:
        raise VersionConflict(pay)
    if pay.status != PaymentStatus.DRAFT:
        raise PaymentError(
            f"payment_not_draft: Payment {pay.id} is {pay.status.value}, not DRAFT"
        )

    # Delegate to the existing posting pipeline (journal creation, number
    # minting, bank/control account wiring, FX delta).
    # post_payment commits internally; we then bump version + write change_log.
    await post_payment(session, payment_id, posted_by=actor)

    # Reload after the commit so we see the latest state.
    pay_loaded = await _get_with_allocations(session, payment_id)
    assert pay_loaded is not None

    pay_loaded.version = pay_loaded.version + 1
    await session.flush()
    await session.refresh(pay_loaded)

    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=pay_loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.PAYMENT_POST,
            table_name="payments",
            row_id=str(pay_loaded.id),
            row_snapshot=_serialise_payment(pay_loaded),
        )
    await change_log_svc.append(
        session,
        entity="payment",
        entity_id=pay_loaded.id,
        op="post",
        actor=actor,
        payload=_serialise_payment(pay_loaded),
        version=pay_loaded.version,
    )
    await session.commit()
    return await _get_with_allocations(session, payment_id)  # type: ignore[return-value]
