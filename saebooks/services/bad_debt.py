"""Bad-debt write-off + recovery service.

Two engine postings, both through the journal chokepoint (``journal_svc.post``)
with a declared ``origin`` — never a hand-authored JE.

Write-off (``write_off_invoice``)
---------------------------------
Writes off the **unpaid balance** of a POSTED invoice as uncollectable. The
GST already remitted on taxable sales is reclaimed as a decreasing adjustment;
GST-free (FRE) sales have nothing to adjust.

    taxable line:   Dr 6-2050 Bad Debts        (ex-GST portion of balance)
                    Dr 2-1310 GST Collected    (GST portion of balance)
                    Cr 1-1200 Trade Debtors    (gross balance)
    GST-free line:  Dr 6-2050 Bad Debts        (full balance portion)
                    Cr 1-1200 Trade Debtors

Mixed-tax invoices are split per line and aggregated into one Dr Bad Debts,
one Dr GST Collected, one Cr AR. For a partially-paid invoice the unpaid
fraction (``balance / total``) is applied per line so only the still-owed GST
is reclaimed. The invoice is marked WRITTEN_OFF with ``amount_paid = total``
so it leaves aged receivables, and ``write_off_journal_entry_id`` is stamped.

Recovery (``record_recovery``)
------------------------------
Money received against a previously written-off debt is assessable income,
**no GST**:

    Dr <bank account>           amount
    Cr 4-1290 Bad Debt Recovery amount   (OTHER_INCOME)

Supports partial / multiple recoveries (paid by the original debtor or a
collection agency). Requires the invoice to be WRITTEN_OFF.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import JournalEntry, JournalOrigin
from saebooks.services import accounts as accounts_svc
from saebooks.services import journal as journal_svc

_TWOPLACES = Decimal("0.01")
_AR_CODE = "1-1200"


class BadDebtError(ValueError):
    """Raised on bad-debt write-off / recovery validation failure."""


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Pure money-split logic (Hypothesis-tested in isolation).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteOffSplit:
    """The dollar split of a write-off across the three GL legs.

    ``ex_gst`` (Dr Bad Debts) + ``gst`` (Dr GST Collected) == ``balance``
    (Cr Trade Debtors), exactly, to the cent. Any proration rounding residual
    is absorbed into ``ex_gst`` so the JE balances without touching the GST
    actually reclaimed beyond sub-cent rounding.
    """

    ex_gst: Decimal
    gst: Decimal
    balance: Decimal


def compute_write_off_split(
    *,
    total: Decimal,
    amount_paid: Decimal,
    line_subtotals: list[Decimal],
    line_taxes: list[Decimal],
) -> WriteOffSplit:
    """Split the unpaid balance into ex-GST (Bad Debts) and GST components.

    ``total`` is the invoice gross; ``amount_paid`` the cash already received.
    ``line_subtotals`` / ``line_taxes`` are the per-line ex-GST and GST
    amounts of the invoice. The unpaid fraction ``balance / total`` is applied
    per line so a partially-paid invoice reclaims only the still-owed GST.

    Invariants (all asserted by the property tests):
      - ``ex_gst + gst == balance`` exactly;
      - ``0 <= gst <= sum(line_taxes)`` (never reclaim more GST than was
        charged);
      - ``balance == total - amount_paid``.

    Raises ``BadDebtError`` when there is no positive balance to write off.
    """
    total = _q2(Decimal(total))
    amount_paid = _q2(Decimal(amount_paid))
    balance = total - amount_paid
    if balance <= Decimal("0"):
        raise BadDebtError(
            f"Nothing to write off — unpaid balance is {balance} "
            f"(total {total}, paid {amount_paid})."
        )
    if total <= Decimal("0"):
        raise BadDebtError(f"Invoice total must be positive, got {total}.")

    fraction = balance / total  # exact ratio; per-line amounts rounded below

    gst_total = Decimal("0")
    ex_gst_total = Decimal("0")
    for subtotal, tax in zip(line_subtotals, line_taxes, strict=True):
        wo_sub = _q2(Decimal(subtotal) * fraction)
        wo_tax = _q2(Decimal(tax) * fraction)
        ex_gst_total += wo_sub
        gst_total += wo_tax

    # Clamp the reclaimed GST so rounding can never push it above the GST
    # actually charged on the invoice (a decreasing adjustment must not
    # over-claim). Then make ex-GST the balancing plug so the three legs tie
    # to the exact balance regardless of per-line rounding.
    max_gst = _q2(sum((Decimal(t) for t in line_taxes), Decimal("0")))
    if gst_total > max_gst:
        gst_total = max_gst
    if gst_total < Decimal("0"):
        gst_total = Decimal("0")
    if gst_total > balance:
        gst_total = balance

    ex_gst_total = balance - gst_total
    return WriteOffSplit(ex_gst=ex_gst_total, gst=gst_total, balance=balance)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _get_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    inv = (
        await session.execute(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise BadDebtError(f"Invoice {invoice_id} not found")
    return inv


async def _get_ar_account(session: AsyncSession, company_id: uuid.UUID) -> Account:
    acct = (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == _AR_CODE,
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise BadDebtError(
            "AR control account 1-1200 Trade Debtors is missing — re-seed the CoA."
        )
    return acct


async def _get_gst_collected_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    """Resolve the GST Collected account via the same settings key the rest of
    the engine uses (mirrors credit_notes._get_gst_collected_account)."""
    from saebooks.services import settings as settings_svc

    code = await settings_svc.get(session, "gst_collected_account_code", "")
    if not code:
        return None
    code = str(code)
    # Settings may store the un-hyphenated form (e.g. "21310"); accept both.
    if "-" not in code and len(code) >= 2 and code[0].isdigit():
        hyphenated = code[0] + "-" + code[1:]
    else:
        hyphenated = code
    return (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code.in_([code, hyphenated]),
                Account.archived_at.is_(None),
            )
        )
    ).scalars().first()


# ---------------------------------------------------------------------------
# Write-off
# ---------------------------------------------------------------------------


async def write_off_invoice(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    invoice_id: uuid.UUID,
    write_off_date: date,
    posted_by: str | None = None,
    reason: str | None = None,
) -> Invoice:
    """Write off a POSTED invoice's unpaid balance as a bad debt.

    Posts ``Dr Bad Debts (+ Dr GST Collected) / Cr Trade Debtors`` via the
    journal chokepoint with ``origin=BAD_DEBT_WRITEOFF``, then marks the
    invoice WRITTEN_OFF with ``amount_paid = total`` (so it leaves aged
    receivables) and stamps ``write_off_journal_entry_id``.
    """
    inv = await _get_invoice(session, invoice_id)
    if inv.company_id != company_id:
        raise BadDebtError(f"Invoice {invoice_id} not found for this company")
    if inv.tenant_id != tenant_id:
        raise BadDebtError(f"Invoice {invoice_id} not found for this tenant")
    if inv.status == InvoiceStatus.WRITTEN_OFF:
        raise BadDebtError(f"Invoice {inv.number or inv.id} is already written off")
    if inv.status != InvoiceStatus.POSTED:
        raise BadDebtError(
            f"Invoice {inv.number or inv.id} is not POSTED "
            f"(status={inv.status.value}); only a posted invoice can be written off"
        )

    split = compute_write_off_split(
        total=inv.total,
        amount_paid=inv.amount_paid,
        line_subtotals=[ln.line_subtotal for ln in inv.lines],
        line_taxes=[ln.line_tax for ln in inv.lines],
    )

    bad_debts_acct = await accounts_svc.get_bad_debt_expense_account(session, company_id)
    ar_acct = await _get_ar_account(session, company_id)

    label = f"Bad debt write-off — invoice {inv.number or inv.id}"

    # Build the JE lines. NB: no gst_amount metadata on any line — the GST
    # Collected leg is posted explicitly (mirrors credit_notes), so
    # gst.auto_post_gst_lines stays a no-op and never double-counts.
    lines: list[dict[str, object]] = [
        {
            "account_id": bad_debts_acct.id,
            "description": label,
            "debit": split.ex_gst,
            "credit": Decimal("0"),
        }
    ]
    if split.gst > Decimal("0"):
        gst_acct = await _get_gst_collected_account(session, company_id)
        if gst_acct is None:
            raise BadDebtError(
                "GST Collected account is not configured (gst_collected_account_code) "
                "but this write-off includes a GST decreasing adjustment."
            )
        lines.append(
            {
                "account_id": gst_acct.id,
                "description": f"{label} — GST decreasing adjustment",
                "debit": split.gst,
                "credit": Decimal("0"),
            }
        )
    lines.append(
        {
            "account_id": ar_acct.id,
            "description": label,
            "debit": Decimal("0"),
            "credit": split.balance,
        }
    )

    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=write_off_date,
        description=label,
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=reason,
        origin=JournalOrigin.BAD_DEBT_WRITEOFF,
        source_type="invoice",
        source_id=inv.id,
    )

    # Settle the invoice the proven way: the JE has cleared AR, so set
    # amount_paid = total and flip status to WRITTEN_OFF. Both the aged-AR
    # report (filters status == POSTED) and the balance_due filter
    # (total > amount_paid) then exclude it.
    inv.amount_paid = inv.total
    inv.base_amount_paid = _q2(inv.total * Decimal(str(inv.fx_rate)))
    inv.status = InvoiceStatus.WRITTEN_OFF
    inv.write_off_journal_entry_id = posted.id
    await session.commit()
    return await _get_invoice(session, invoice_id)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


async def record_recovery(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    invoice_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    amount: Decimal,
    recovery_date: date,
    posted_by: str | None = None,
    payer_contact_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Record money received against a written-off debt as Other Income.

    Posts ``Dr <bank> / Cr 4-1290 Bad Debt Recovery`` with NO GST and
    ``origin=BAD_DEBT_RECOVERY``, linked back to the written-off invoice.
    Supports partial / repeated recoveries (no cap to the original debt);
    the payer may differ from the debtor (e.g. a collection agency).
    """
    amount = _q2(Decimal(amount))
    if amount <= Decimal("0"):
        raise BadDebtError(f"Recovery amount must be positive, got {amount}.")

    inv = await _get_invoice(session, invoice_id)
    if inv.company_id != company_id:
        raise BadDebtError(f"Invoice {invoice_id} not found for this company")
    if inv.tenant_id != tenant_id:
        raise BadDebtError(f"Invoice {invoice_id} not found for this tenant")
    if inv.status != InvoiceStatus.WRITTEN_OFF:
        raise BadDebtError(
            f"Invoice {inv.number or inv.id} is not WRITTEN_OFF "
            f"(status={inv.status.value}); recovery only applies to a "
            f"written-off debt"
        )

    bank_acct = (
        await session.execute(
            select(Account).where(
                Account.id == bank_account_id,
                Account.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if bank_acct is None:
        raise BadDebtError(
            f"Bank account {bank_account_id} not found for this company"
        )

    recovery_acct = await accounts_svc.get_bad_debt_recovery_account(session, company_id)

    label = f"Bad debt recovery — invoice {inv.number or inv.id}"
    lines: list[dict[str, object]] = [
        {
            "account_id": bank_acct.id,
            "description": label,
            "debit": amount,
            "credit": Decimal("0"),
        },
        {
            "account_id": recovery_acct.id,
            "description": label,
            "debit": Decimal("0"),
            "credit": amount,
        },
    ]

    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=recovery_date,
        description=label,
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        origin=JournalOrigin.BAD_DEBT_RECOVERY,
        source_type="invoice",
        source_id=inv.id,
    )
    await session.commit()
    return posted


__all__ = [
    "BadDebtError",
    "WriteOffSplit",
    "compute_write_off_split",
    "record_recovery",
    "write_off_invoice",
]
