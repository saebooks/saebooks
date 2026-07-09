"""Period close — zero out P&L into retained earnings + lock the period.

Classical year-end entry:

* **For every INCOME / OTHER_INCOME account** with a non-zero balance
  on the closing date, post a debit equal to the credit-normal balance.
* **For every EXPENSE / COST_OF_SALES / OTHER_EXPENSE account** with a
  non-zero balance, post a credit equal to the debit-normal balance.
* **Plug the balance** to a single equity account — ``Retained Earnings``
  (or an equivalent). If income exceeded expenses the balance is a
  credit (profit → increases retained earnings). If expenses exceeded
  income, the balance is a debit (loss → decreases retained earnings).

After the journal posts, a ``PeriodLock`` is added through
``journal.lock_period`` so nobody can sneak a late entry into the
closed year.

The service is **idempotent by construction**: the preview step
computes balances from POSTED entries only (which excludes DRAFT
entries and anything after ``through_date``). Running it twice in a
row produces two journals if the user doesn't check the preview — the
first one zeroes the period; the second one finds all-zero balances
and posts nothing. The caller is expected to build a UI around the
preview to prevent that in practice.

Callers:
- ``/reports/close-year`` — the UI form (Batch AA router)
- cron job (future) — for fully unattended year-end in multi-tenant
  Enterprise builds.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import AccountType
from saebooks.models.journal import JournalEntry, JournalOrigin
from saebooks.services import journal as journal_svc
from saebooks.services.reports import PNL_TYPES, _account_balances


class PeriodCloseError(Exception):
    """Raised when the period cannot be closed — missing account,
    zero net profit with no lines to post, etc."""


@dataclass
class ClosePreview:
    """What ``close_year`` WOULD post if called with the same args.

    ``retained_earnings_debit`` / ``retained_earnings_credit`` carry
    the plug — exactly one will be non-zero (or both zero when income
    equals expenses exactly). ``net_profit`` is credit-positive (a
    profit is positive, a loss is negative) so the UI can render
    "Net profit: $X" without post-processing.
    """

    through_date: date
    total_income: Decimal = Decimal("0")
    total_expenses: Decimal = Decimal("0")
    lines: list[dict[str, object]] = field(default_factory=list)
    retained_earnings_debit: Decimal = Decimal("0")
    retained_earnings_credit: Decimal = Decimal("0")

    @property
    def net_profit(self) -> Decimal:
        return self.total_income - self.total_expenses

    @property
    def has_anything_to_close(self) -> bool:
        # If every P&L account is zero, net_profit is zero AND lines is
        # empty — posting would be a no-op.
        return bool(self.lines)


async def preview_close(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    through_date: date,
    retained_earnings_account_id: uuid.UUID,
    from_date: date | None = None,
) -> ClosePreview:
    """Compute the zeroing entry WITHOUT posting it.

    ``from_date`` defaults to None, which means the journal aggregates
    over every posted P&L entry up to ``through_date``. For a mid-year
    close or a partial-period close, set ``from_date`` explicitly.

    The lines list is in the shape ``journal.create_draft`` accepts —
    ``[{"account_id": ..., "debit": ..., "credit": ...}, ...]``.
    """
    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=through_date
    )
    pnl = [b for b in balances if b.account_type in PNL_TYPES]

    preview = ClosePreview(through_date=through_date)
    total_income = Decimal("0")
    total_expenses = Decimal("0")

    for b in pnl:
        # Skip all-zero accounts. `balance` is (debit - credit); income
        # rolls up as a negative (credit-normal) and expenses as a
        # positive (debit-normal).
        if b.debit == Decimal("0") and b.credit == Decimal("0"):
            continue
        net = b.balance  # debit - credit
        if b.account_type in {AccountType.INCOME, AccountType.OTHER_INCOME}:
            # Income accounts carry credit-normal balances: `net` is
            # negative. Debit the account by |net| to zero it.
            amount = -net  # flip sign so it's positive
            if amount == Decimal("0"):
                continue
            preview.lines.append(
                {
                    "account_id": b.account_id,
                    "description": f"Close P&L — {b.code} {b.name}",
                    "debit": amount,
                    "credit": Decimal("0"),
                }
            )
            total_income += amount
        else:
            # Expense-family (EXPENSE / COST_OF_SALES / OTHER_EXPENSE) —
            # debit-normal balance: `net` is positive. Credit the
            # account by `net` to zero it.
            amount = net
            if amount == Decimal("0"):
                continue
            preview.lines.append(
                {
                    "account_id": b.account_id,
                    "description": f"Close P&L — {b.code} {b.name}",
                    "debit": Decimal("0"),
                    "credit": amount,
                }
            )
            total_expenses += amount

    preview.total_income = total_income
    preview.total_expenses = total_expenses

    # Plug to retained earnings. Net profit > 0 means credit-heavy
    # (income > expenses) → credit retained earnings; net profit < 0
    # means debit retained earnings. If exactly zero, no plug line.
    net_profit = total_income - total_expenses
    if net_profit > Decimal("0"):
        preview.retained_earnings_credit = net_profit
        preview.lines.append(
            {
                "account_id": retained_earnings_account_id,
                "description": "Net profit → Retained Earnings",
                "debit": Decimal("0"),
                "credit": net_profit,
            }
        )
    elif net_profit < Decimal("0"):
        amount = -net_profit
        preview.retained_earnings_debit = amount
        preview.lines.append(
            {
                "account_id": retained_earnings_account_id,
                "description": "Net loss → Retained Earnings",
                "debit": amount,
                "credit": Decimal("0"),
            }
        )
    # If net_profit == 0 and there are P&L lines, they already balance
    # among themselves (income dr == expenses cr), so no plug needed.

    return preview


async def close_year(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    through_date: date,
    retained_earnings_account_id: uuid.UUID,
    posted_by: str | None = None,
    from_date: date | None = None,
    lock_period: bool = True,
    override_reason: str | None = None,
    actor_role: str | None = None,
) -> JournalEntry | None:
    """Build + post the year-end close journal, then lock the period.

    Returns the posted JournalEntry, or ``None`` if the preview had
    nothing to close (every P&L account is already zero — common on a
    fresh DB or immediately after a prior close).

    Set ``lock_period=False`` when running from a test that wants to
    post further journals in the same period — the lock blocks
    additional posts without an override reason.

    ``actor_role`` is the F-04 period-lock override role. Year-end close
    routinely posts INTO a date that is immediately about to be locked,
    so callers from an admin-gated route must pass ``actor_role="admin"``
    (or accountant/owner). Callers that don't supply a role cannot
    override a pre-existing lock at ``through_date`` — fail-closed.
    """
    preview = await preview_close(
        session,
        company_id,
        through_date=through_date,
        retained_earnings_account_id=retained_earnings_account_id,
        from_date=from_date,
    )
    if not preview.has_anything_to_close:
        return None

    description = (
        f"Year-end close through {through_date.isoformat()} — "
        f"net profit {preview.net_profit}"
    )
    draft = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=through_date,
        description=description,
        lines=preview.lines,
    )
    posted = await journal_svc.post(
        session,
        draft.id,
        posted_by=posted_by,
        override_reason=override_reason,
        actor_role=actor_role,
        # Year-end close rolls up many P&L accounts to retained earnings —
        # no single originating record, so source_type/id stay null.
        origin=JournalOrigin.YEAR_END_CLOSE,
    )

    if lock_period:
        await journal_svc.lock_period(
            session,
            company_id,
            through_date,
            locked_by=posted_by,
            reason=f"Year-end close: {description}",
        )

    return posted


__all__ = [
    "ClosePreview",
    "PeriodCloseError",
    "close_year",
    "preview_close",
]
