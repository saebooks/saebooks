"""Edition / bookkeeping-mode helpers.

The ``Company.bookkeeping_mode`` column is a UX-level flag:

* ``full``      — accrual accounting; invoices post A/R on issue.
* ``cashbook``  — cash-basis UX for sole traders; invoices DO NOT post
                  to the GL on issue. They are document-only records
                  until payment lands, at which point a single
                  combined Dr Bank / Cr Income / Cr GST entry is posted
                  (no A/R intermediary). Symmetrical to the existing
                  expense flow (Dr Expense / Cr Bank — no A/P).

This module owns the mode look-up and the cashbook→full backfill that
synthesises Dr A/R / Cr Income / Cr GST journal entries for any
invoices that are still OPEN at flip time. Already-paid invoices need
no backfill: the cashbook-mode payment posted Dr Bank / Cr Income / Cr
GST, which is net-equivalent to the full-mode pair (issue: Dr A/R / Cr
Income / Cr GST) + (settle: Dr Bank / Cr A/R) once A/R nets to zero.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.invoice import Invoice, InvoiceStatus


_AR_CODE = "1-1200"


async def is_cashbook_mode(
    session: AsyncSession, company_id: uuid.UUID
) -> bool:
    """Return True iff the company is currently in cashbook mode.

    Resolves with a single SELECT — caller passes the company id, we
    fetch only ``bookkeeping_mode``. ``False`` is returned for unknown
    companies so the safer "full" path runs (defensive default).
    """
    result = await session.execute(
        select(Company.bookkeeping_mode).where(Company.id == company_id)
    )
    mode = result.scalar_one_or_none()
    return mode == "cashbook"


async def list_open_invoices_for_backfill(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Invoice]:
    """Return POSTED invoices with outstanding balance and no JE.

    Used by ``upgrade_cashbook_to_full`` to find invoices that need an
    A/R-on-issue JE synthesised before the mode flips. Filter:

    * ``status == POSTED``  — drafts and voided excluded
    * ``journal_entry_id IS NULL`` — already-backfilled invoices skip
    * ``amount_paid < total`` — fully-paid invoices need no backfill
    """
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.journal_entry_id.is_(None),
            Invoice.amount_paid < Invoice.total,
        )
        .order_by(Invoice.issue_date)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def backfill_invoice_journals(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    actor: str | None = None,
) -> int:
    """Synthesise Dr A/R / Cr Income / Cr GST JEs for open invoices.

    Returns the number of invoices backfilled. Posts each invoice's JE
    on the invoice's own ``issue_date`` so the ledger reflects the
    original issue date. Stamps ``journal_entry_id`` on each invoice.

    Idempotent: only invoices with NULL ``journal_entry_id`` are
    touched. Re-running the backfill is a no-op once each invoice has
    its JE.

    Caller is responsible for committing the session after this returns.
    """
    from saebooks.services import journal as journal_svc

    invoices = await list_open_invoices_for_backfill(session, company_id)
    if not invoices:
        return 0

    # AR control account (1-1200) is required.
    ar_stmt = select(Account).where(
        Account.company_id == company_id, Account.code == _AR_CODE
    )
    ar = (await session.execute(ar_stmt)).scalar_one_or_none()
    if ar is None:
        raise ValueError(
            "Trade Debtors (1-1200) not found in chart of accounts — "
            "re-run the AU CoA seed before upgrading to full mode."
        )

    count = 0
    for inv in invoices:
        rate = Decimal(str(inv.fx_rate or Decimal("1")))
        base_total = Decimal(str(inv.base_total or inv.total))
        je_lines: list[dict[str, object]] = [
            {
                "account_id": ar.id,
                "description": f"Invoice {inv.number} (cashbook→full backfill)",
                "debit": base_total,
                "credit": Decimal("0"),
            },
        ]
        for ln in inv.lines:
            line_base = Decimal(str(ln.line_subtotal or 0)) * rate
            line_tax = (
                Decimal(str(ln.line_tax or 0)) * rate
                if ln.line_tax and Decimal(str(ln.line_tax)) > 0
                else None
            )
            if line_base <= Decimal("0"):
                continue
            je_lines.append(
                {
                    "account_id": ln.account_id,
                    "description": f"{inv.number}: {ln.description}",
                    "debit": Decimal("0"),
                    "credit": line_base.quantize(Decimal("0.01")),
                    "tax_code_id": ln.tax_code_id,
                    "gst_amount": (
                        line_tax.quantize(Decimal("0.01"))
                        if line_tax is not None
                        else None
                    ),
                    "project_id": ln.project_id,
                }
            )

        entry = await journal_svc.create_draft(
            session,
            company_id=inv.company_id,
            entry_date=inv.issue_date,
            description=f"Invoice {inv.number} (backfill from cashbook)",
            lines=je_lines,
        )
        posted = await journal_svc.post(
            session,
            entry.id,
            posted_by=actor,
            override_reason="cashbook_to_full_backfill",
        )
        inv.journal_entry_id = posted.id
        if inv.posted_at is None:
            inv.posted_at = datetime.now(UTC)
        count += 1
    return count


__all__ = [
    "is_cashbook_mode",
    "list_open_invoices_for_backfill",
    "backfill_invoice_journals",
]
