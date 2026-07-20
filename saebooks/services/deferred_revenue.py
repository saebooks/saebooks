"""Deferred-revenue recognition service (FITC-3).

When an invoice line has service_start_date and service_end_date that span
more than one calendar month, posting routes the income credit to Unearned
Income (2-1760) rather than the income account directly.

``recognize_deferred_revenue`` is called at period close (or on demand) to
move the earned portion from Unearned Income into the appropriate income
account for that month:

    Dr Unearned Income (2-1760) ... monthly_amount
    Cr Income account ............. monthly_amount

Monthly amount = line_subtotal / total_calendar_months, with the last month
absorbing any rounding remainder so the total recognized equals line_subtotal
exactly.

``recognized_through_date`` on the invoice line tracks the last period
(stored as the first day of that month) for which recognition has run.
The query excludes lines where recognized_through_date >= this period's
first day, so the function is idempotent for a given period.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import JournalOrigin
from saebooks.money import round_money
from saebooks.services import journal as journal_svc
from saebooks.services import settings as settings_svc


class DeferredRevenueError(ValueError):
    pass


def _q2(v: Decimal) -> Decimal:
    return round_money(v)


def _period_first(d: date) -> date:
    return date(d.year, d.month, 1)


def _total_months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _monthly_amount(line_subtotal: Decimal, start: date, end: date, period: date) -> Decimal:
    total = _total_months(start, end)
    per_month = _q2(line_subtotal / Decimal(total))
    # Last month absorbs rounding remainder
    if (period.year, period.month) == (end.year, end.month):
        return _q2(line_subtotal - per_month * Decimal(total - 1))
    return per_month


@dataclass
class DeferredRecognitionPreview:
    period_first: date
    lines: list[dict[str, object]] = field(default_factory=list)
    total_recognized: Decimal = Decimal("0")

    @property
    def has_entries(self) -> bool:
        return bool(self.lines)


async def _get_account_by_code(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> Account | None:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == code,
            Account.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _get_gst_collected_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    code = await settings_svc.get(session, "gst_collected_account_code", "")
    if not code:
        return None
    return await _get_account_by_code(session, company_id, str(code))


async def _deferred_lines_for_period(
    session: AsyncSession,
    company_id: uuid.UUID,
    period_first_day: date,
) -> list[InvoiceLine]:
    """Return all posted invoice lines whose service period includes this month
    and that haven't been recognized for this period yet."""
    # Last day of the period month (service_start_date must be <= this)
    if period_first_day.month == 12:
        period_last_day = date(period_first_day.year + 1, 1, 1)
    else:
        period_last_day = date(period_first_day.year, period_first_day.month + 1, 1)
    # period_last_day is actually the first of next month; service_start_date
    # must be < that (i.e. starts on or before the last day of this month)

    stmt = (
        select(InvoiceLine)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(InvoiceLine.invoice))
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            InvoiceLine.service_start_date.is_not(None),
            InvoiceLine.service_end_date.is_not(None),
            # Service starts before end of this period
            InvoiceLine.service_start_date < period_last_day,
            # Service ends on or after start of this period
            InvoiceLine.service_end_date >= period_first_day,
            # Not yet recognized for this period
            or_(
                InvoiceLine.recognized_through_date.is_(None),
                InvoiceLine.recognized_through_date < period_first_day,
            ),
            # Only multi-month lines (same month = not deferred)
            # We can't express this purely in SQL without computed columns,
            # so we filter in Python below.
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    # Filter: only truly deferred lines (span > 1 calendar month)
    return [
        r for r in rows
        if r.service_start_date is not None
        and r.service_end_date is not None
        and (r.service_start_date.year, r.service_start_date.month)
        != (r.service_end_date.year, r.service_end_date.month)
    ]


async def preview_deferred_recognition(
    session: AsyncSession,
    company_id: uuid.UUID,
    period_date: date,
) -> DeferredRecognitionPreview:
    """Compute what ``recognize_deferred_revenue`` would post, without posting."""
    pf = _period_first(period_date)
    lines = await _deferred_lines_for_period(session, company_id, pf)
    preview = DeferredRecognitionPreview(period_first=pf)
    for line in lines:
        assert line.service_start_date is not None
        assert line.service_end_date is not None
        amt = _monthly_amount(
            line.line_subtotal, line.service_start_date, line.service_end_date, pf
        )
        if amt <= Decimal("0"):
            continue
        inv_number = line.invoice.number if line.invoice else str(line.invoice_id)
        preview.lines.append({
            "invoice_line_id": line.id,
            "invoice_number": inv_number,
            "description": line.description,
            "income_account_id": line.account_id,
            "amount": amt,
        })
        preview.total_recognized += amt
    return preview


async def recognize_deferred_revenue(
    session: AsyncSession,
    company_id: uuid.UUID,
    period_date: date,
    *,
    tenant_id: uuid.UUID,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> None:
    """Post monthly amortization JEs for all deferred invoice lines whose service
    period overlaps ``period_date``'s calendar month.

    For each qualifying line:
        Dr Unearned Income (2-1760) ... monthly_amount
        Cr Income account ............. monthly_amount

    Stamps ``recognized_through_date`` on each line so repeat calls for the
    same period are no-ops.

    Returns nothing (callers inspect the posted JEs via the journal ledger).
    """
    pf = _period_first(period_date)
    lines = await _deferred_lines_for_period(session, company_id, pf)
    if not lines:
        return

    unearned_acct = await _get_account_by_code(session, company_id, "2-1760")
    if unearned_acct is None:
        raise DeferredRevenueError(
            "Unearned Income account 2-1760 is missing — re-run the CoA seed."
        )

    je_lines: list[dict[str, object]] = []

    for line in lines:
        assert line.service_start_date is not None
        assert line.service_end_date is not None
        amt = _monthly_amount(
            line.line_subtotal, line.service_start_date, line.service_end_date, pf
        )
        if amt <= Decimal("0"):
            continue
        inv_number = line.invoice.number if line.invoice else str(line.invoice_id)
        desc = f"Deferred rev {pf.strftime('%b %Y')} — {inv_number}: {line.description}"
        je_lines.append({
            "account_id": unearned_acct.id,
            "description": desc,
            "debit": amt,
            "credit": Decimal("0"),
        })
        je_lines.append({
            "account_id": line.account_id,
            "description": desc,
            "debit": Decimal("0"),
            "credit": amt,
        })
        # Mark recognized for this period
        line.recognized_through_date = pf

    if not je_lines:
        return

    draft = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=pf,
        description=f"Deferred revenue recognition — {pf.strftime('%B %Y')}",
        lines=je_lines,
    )
    await journal_svc.post(
        session,
        draft.id,
        posted_by=posted_by,
        override_reason=override_reason or f"Deferred revenue recognition {pf.isoformat()}",
        # Recognition spans many deferred-revenue lines in one period roll —
        # no single originating record, so source_type/id stay null.
        origin=JournalOrigin.DEFERRED_REVENUE,
    )
    await session.commit()


__all__ = [
    "DeferredRecognitionPreview",
    "DeferredRevenueError",
    "preview_deferred_recognition",
    "recognize_deferred_revenue",
]
