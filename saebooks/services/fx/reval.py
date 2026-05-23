"""FX revaluation — month-end unrealised gain/loss on open AR/AP.

Context
-------
Batch GG/2 shipped *realised* FX: when a foreign-currency payment
settles a foreign-currency invoice/bill and the rates differ, the
difference between the base-currency receipt and the base-currency
control posts to ``6-1640`` / ``6-1630``. That handles settlement —
but between issue and settlement the open AR / AP still carries the
*issue-date* rate, so the balance sheet is stale during the month.

This module adds the classical-accounting fix: on a chosen revaluation
date (typically month-end) we compute the unrealised FX position for
each foreign currency and post an *adjusting* journal to restate open
AR / AP to the period-end rate, plus a *reversing* journal dated the
following day. The reversal makes the adjustment ephemeral — next
period starts fresh, and when the real settlement eventually posts
its realised FX it isn't "double-counted" against the month-end
snapshot.

Math
----
For a foreign currency ``C`` (≠ base) on ``through_date``::

    outstanding_foreign_AR  = Σ (invoice.total - invoice.amount_paid)
                              for open, POSTED, not-archived invoices
                              in currency C

    current_base_AR         = Σ (invoice.base_total - invoice.base_amount_paid)
                              for the same set

    new_rate                = get_rate(C → base, through_date)
    revalued_base_AR        = outstanding_foreign_AR * new_rate
    ar_delta                = revalued_base_AR - current_base_AR

``ar_delta > 0`` means base-currency value of the AR climbed — we'd
collect more base than the books currently show — post
``Dr 1-1200 AR`` / ``Cr 6-1640 FX Gain`` for ``ar_delta``. Loss is the
mirror: ``Dr 6-1630 FX Loss`` / ``Cr 1-1200 AR``.

AP is the symmetric case: ``ap_delta > 0`` means we owe *more* base,
so post ``Dr 6-1630 FX Loss`` / ``Cr 2-1200 AP``.

All four deltas for one currency land on one combined journal, so a
currency with both AR and AP exposure posts a single adjustment (and
one reversal) rather than two of each.

Idempotency
-----------
Both posted journals are tagged via ``JournalEntry.attachments``::

    {
        "kind": "fx_reval",
        "currency": "USD",
        "through_date": "2026-03-31",
        "side": "adjustment" | "reversal",
    }

Re-running ``revalue_company(company, through_date=X)`` queries the
adjustments already posted for that ``(company, currency, X)`` tuple
and skips the currency if one exists. The CLI and router lean on this
— running the same month twice is a no-op, not a double-post.

Non-goals for v1
----------------
* FX-denominated bank accounts are not revalued (open AR/AP only).
  Banks typically settle daily so the realised path covers it; true
  bank reval is a follow-up once multi-currency bank accounts are
  actually in use.
* Partial-period reval (e.g. 15th of the month): technically works,
  the service doesn't care about dates, but the reversal always lands
  on ``through_date + 1`` which may not be what the user wants for a
  mid-month snapshot. Document the behaviour in the UI.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import JournalEntry
from saebooks.services import journal as journal_svc
from saebooks.services.fx.rates import get_rate

log = logging.getLogger("saebooks.fx.reval")

_AR_CODE = "1-1200"   # Trade Debtors
_AP_CODE = "2-1200"   # Trade Creditors
_FX_GAIN_CODE = "6-1640"  # Exchange Rate Gain
_FX_LOSS_CODE = "6-1630"  # Exchange Rate Loss
_TWOPLACES = Decimal("0.01")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


class FxRevalError(RuntimeError):
    """Raised when revaluation can't proceed (missing CoA row, etc.)."""


@dataclass(frozen=True)
class CurrencyReval:
    """Per-currency revaluation numbers.

    Nothing posted yet — just the math. ``is_zero`` is True when both
    AR and AP deltas round to zero; we skip the post entirely in that
    case (no point writing zero-line journals).
    """

    currency: str
    new_rate: Decimal
    outstanding_foreign_ar: Decimal
    current_base_ar: Decimal
    revalued_base_ar: Decimal
    ar_delta: Decimal
    outstanding_foreign_ap: Decimal
    current_base_ap: Decimal
    revalued_base_ap: Decimal
    ap_delta: Decimal

    @property
    def is_zero(self) -> bool:
        return self.ar_delta == Decimal("0") and self.ap_delta == Decimal("0")


@dataclass(frozen=True)
class RevalResult:
    """Result of one revaluation run for one company.

    Attributes
    ----------
    through_date:
        The revaluation date requested. Adjusting journals carry this
        date; reversals carry ``through_date + 1``.
    entries:
        Pairs of ``(adjustment_entry_id, reversal_entry_id)`` posted in
        this run. Empty list = nothing to post.
    skipped_currencies:
        Currencies that had open exposure but were already revalued on
        this ``through_date`` — idempotent re-run skips them silently.
    zero_currencies:
        Currencies with exposure but zero delta (rate unchanged from
        current blended average).
    """

    through_date: date
    entries: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    skipped_currencies: list[str] = field(default_factory=list)
    zero_currencies: list[str] = field(default_factory=list)

    @property
    def posted_count(self) -> int:
        return len(self.entries)


# --------------------------------------------------------------------- #
# Queries                                                                #
# --------------------------------------------------------------------- #


async def _open_invoices_by_currency(
    session: AsyncSession, company_id: uuid.UUID, through_date: date
) -> dict[str, list[Invoice]]:
    """Open POSTED invoices at ``through_date`` grouped by currency.

    "Open" = issued on or before ``through_date``, POSTED, not archived,
    with ``total > amount_paid``. VOIDED invoices are excluded by the
    status filter.
    """
    stmt = (
        select(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.archived_at.is_(None),
            Invoice.issue_date <= through_date,
            Invoice.total > Invoice.amount_paid,
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[str, list[Invoice]] = {}
    for inv in rows:
        grouped.setdefault(inv.currency.upper(), []).append(inv)
    return grouped


async def _open_bills_by_currency(
    session: AsyncSession, company_id: uuid.UUID, through_date: date
) -> dict[str, list[Bill]]:
    """Open POSTED bills at ``through_date`` grouped by currency."""
    stmt = (
        select(Bill)
        .where(
            Bill.company_id == company_id,
            Bill.status == BillStatus.POSTED,
            Bill.archived_at.is_(None),
            Bill.issue_date <= through_date,
            Bill.total > Bill.amount_paid,
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[str, list[Bill]] = {}
    for bill in rows:
        grouped.setdefault(bill.currency.upper(), []).append(bill)
    return grouped


async def _load_account(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> Account:
    acct = (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id, Account.code == code
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise FxRevalError(
            f"Account {code} missing — FX revaluation needs AR/AP control + "
            f"Exchange Rate Gain/Loss accounts from the seed"
        )
    return acct


async def _existing_reval_journal(
    session: AsyncSession,
    company_id: uuid.UUID,
    currency: str,
    through_date: date,
    side: str = "adjustment",
) -> JournalEntry | None:
    """Return an existing reval journal for this (company, ccy, date, side) tuple.

    Matches on the JSONB ``attachments`` tag we stamp on every reval
    post. Idempotency hook — callers skip the currency if this returns
    non-None for ``side="adjustment"``.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.attachments["kind"].as_string() == "fx_reval",
            JournalEntry.attachments["currency"].as_string() == currency,
            JournalEntry.attachments["through_date"].as_string()
            == through_date.isoformat(),
            JournalEntry.attachments["side"].as_string() == side,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------- #
# Pure math                                                              #
# --------------------------------------------------------------------- #


def _sum_outstanding(
    invs_or_bills: list[Invoice] | list[Bill],
) -> tuple[Decimal, Decimal]:
    """Return ``(foreign_total_outstanding, base_total_outstanding)``.

    Foreign = ``total - amount_paid`` summed in document currency.
    Base = ``base_total - base_amount_paid`` summed in base currency.
    Both rounded to 2dp at the end.
    """
    foreign = Decimal("0")
    base = Decimal("0")
    for row in invs_or_bills:
        foreign += row.total - row.amount_paid
        base += row.base_total - row.base_amount_paid
    return _q2(foreign), _q2(base)


def _reval_for_currency(
    currency: str,
    new_rate: Decimal,
    invoices: list[Invoice],
    bills: list[Bill],
) -> CurrencyReval:
    """Compute the per-currency reval numbers. Pure — no DB."""
    ar_foreign, ar_base = _sum_outstanding(invoices)
    ap_foreign, ap_base = _sum_outstanding(bills)

    revalued_ar = _q2(ar_foreign * new_rate)
    revalued_ap = _q2(ap_foreign * new_rate)

    return CurrencyReval(
        currency=currency,
        new_rate=new_rate,
        outstanding_foreign_ar=ar_foreign,
        current_base_ar=ar_base,
        revalued_base_ar=revalued_ar,
        ar_delta=revalued_ar - ar_base,
        outstanding_foreign_ap=ap_foreign,
        current_base_ap=ap_base,
        revalued_base_ap=revalued_ap,
        ap_delta=revalued_ap - ap_base,
    )


def _build_reval_lines(
    *,
    ar_account_id: uuid.UUID,
    ap_account_id: uuid.UUID,
    gain_account_id: uuid.UUID,
    loss_account_id: uuid.UUID,
    ar_delta: Decimal,
    ap_delta: Decimal,
    currency: str,
) -> list[dict[str, object]]:
    """Build the balanced line-dict list for one currency's adjustment.

    AR side
    -------
    ``ar_delta > 0`` → revalued AR is higher (gain).
        Dr AR control / Cr Exchange Rate Gain.
    ``ar_delta < 0`` → revalued AR is lower (loss).
        Dr Exchange Rate Loss / Cr AR control.

    AP side
    -------
    ``ap_delta > 0`` → we owe more base (loss).
        Dr Exchange Rate Loss / Cr AP control.
    ``ap_delta < 0`` → we owe less base (gain).
        Dr AP control / Cr Exchange Rate Gain.
    """
    lines: list[dict[str, object]] = []

    if ar_delta > Decimal("0"):
        lines.append({
            "account_id": ar_account_id,
            "description": f"FX reval {currency} AR gain",
            "debit": ar_delta,
            "credit": Decimal("0"),
        })
        lines.append({
            "account_id": gain_account_id,
            "description": f"FX reval {currency} AR gain",
            "debit": Decimal("0"),
            "credit": ar_delta,
        })
    elif ar_delta < Decimal("0"):
        mag = -ar_delta
        lines.append({
            "account_id": loss_account_id,
            "description": f"FX reval {currency} AR loss",
            "debit": mag,
            "credit": Decimal("0"),
        })
        lines.append({
            "account_id": ar_account_id,
            "description": f"FX reval {currency} AR loss",
            "debit": Decimal("0"),
            "credit": mag,
        })

    if ap_delta > Decimal("0"):
        mag = ap_delta
        lines.append({
            "account_id": loss_account_id,
            "description": f"FX reval {currency} AP loss",
            "debit": mag,
            "credit": Decimal("0"),
        })
        lines.append({
            "account_id": ap_account_id,
            "description": f"FX reval {currency} AP loss",
            "debit": Decimal("0"),
            "credit": mag,
        })
    elif ap_delta < Decimal("0"):
        mag = -ap_delta
        lines.append({
            "account_id": ap_account_id,
            "description": f"FX reval {currency} AP gain",
            "debit": mag,
            "credit": Decimal("0"),
        })
        lines.append({
            "account_id": gain_account_id,
            "description": f"FX reval {currency} AP gain",
            "debit": Decimal("0"),
            "credit": mag,
        })

    return lines


def _reverse_lines(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    """Swap debit/credit on every line — reversal journal shape."""
    reversed_: list[dict[str, object]] = []
    for ln in lines:
        reversed_.append({
            "account_id": ln["account_id"],
            "description": ln["description"],
            "debit": ln["credit"],
            "credit": ln["debit"],
        })
    return reversed_


# --------------------------------------------------------------------- #
# Post                                                                   #
# --------------------------------------------------------------------- #


async def _post_reval_pair(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reval: CurrencyReval,
    through_date: date,
    ar_account: Account,
    ap_account: Account,
    gain_account: Account,
    loss_account: Account,
    posted_by: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Post the adjusting + reversing journal pair for one currency."""
    lines = _build_reval_lines(
        ar_account_id=ar_account.id,
        ap_account_id=ap_account.id,
        gain_account_id=gain_account.id,
        loss_account_id=loss_account.id,
        ar_delta=reval.ar_delta,
        ap_delta=reval.ap_delta,
        currency=reval.currency,
    )
    if not lines:
        raise FxRevalError(
            f"Refusing to post empty reval journal for {reval.currency} on "
            f"{through_date} — caller should check CurrencyReval.is_zero first"
        )

    # Adjusting journal — dated through_date, tagged adjustment.
    adjustment = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=through_date,
        description=(
            f"FX revaluation {reval.currency} @ {reval.new_rate} "
            f"through {through_date.isoformat()}"
        ),
        lines=lines,
    )
    adjustment.attachments = {
        "kind": "fx_reval",
        "currency": reval.currency,
        "through_date": through_date.isoformat(),
        "side": "adjustment",
    }
    # The post() call will bypass the balance check only if GST lines
    # are involved — our reval lines never carry a tax_code_id so the
    # existing balance check covers us.
    await session.commit()
    adjustment = await journal_svc.post(
        session, adjustment.id, posted_by=posted_by
    )

    # Reversing journal — dated through_date + 1, opposite signs.
    rev_date = through_date + timedelta(days=1)
    reversal = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=rev_date,
        description=(
            f"FX revaluation {reval.currency} reversal of "
            f"{through_date.isoformat()}"
        ),
        lines=_reverse_lines(lines),
    )
    reversal.attachments = {
        "kind": "fx_reval",
        "currency": reval.currency,
        "through_date": through_date.isoformat(),
        "side": "reversal",
    }
    await session.commit()
    reversal = await journal_svc.post(
        session, reversal.id, posted_by=posted_by
    )

    return adjustment.id, reversal.id


# --------------------------------------------------------------------- #
# Public entry points                                                    #
# --------------------------------------------------------------------- #


async def preview_company(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    through_date: date,
    source: str = "rba",
    base_currency: str = "AUD",
) -> list[CurrencyReval]:
    """Return per-currency reval numbers WITHOUT posting.

    Skips ``base_currency`` (no point revaluing AUD→AUD). Raises
    ``FxRateError`` if a rate for an open foreign currency can't be
    resolved — callers surface this in the UI so the user knows which
    pair needs a fetcher or a seed row.
    """
    base_currency = base_currency.upper()
    invoices_by_ccy = await _open_invoices_by_currency(
        session, company_id, through_date
    )
    bills_by_ccy = await _open_bills_by_currency(
        session, company_id, through_date
    )

    currencies = set(invoices_by_ccy.keys()) | set(bills_by_ccy.keys())
    currencies.discard(base_currency)

    revals: list[CurrencyReval] = []
    for ccy in sorted(currencies):
        rate = await get_rate(
            session,
            from_ccy=ccy,
            to_ccy=base_currency,
            as_of=through_date,
            source=source,
        )
        reval = _reval_for_currency(
            ccy,
            rate,
            invoices_by_ccy.get(ccy, []),
            bills_by_ccy.get(ccy, []),
        )
        revals.append(reval)

    return revals


async def revalue_company(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    through_date: date,
    source: str = "rba",
    base_currency: str = "AUD",
    posted_by: str | None = None,
) -> RevalResult:
    """Post adjusting + reversing journals for every open foreign currency.

    Idempotent — a currency with an existing ``fx_reval`` adjustment
    on ``through_date`` is skipped (no new journals). Zero-delta
    currencies are reported in ``zero_currencies`` but also skipped.
    Every currency with non-zero exposure posts exactly one pair per
    call.
    """
    revals = await preview_company(
        session,
        company_id=company_id,
        through_date=through_date,
        source=source,
        base_currency=base_currency,
    )

    # Pre-load the four control accounts once — reused across currencies.
    ar_account = await _load_account(session, company_id, _AR_CODE)
    ap_account = await _load_account(session, company_id, _AP_CODE)
    gain_account = await _load_account(session, company_id, _FX_GAIN_CODE)
    loss_account = await _load_account(session, company_id, _FX_LOSS_CODE)

    result = RevalResult(through_date=through_date)

    for reval in revals:
        if reval.is_zero:
            result.zero_currencies.append(reval.currency)
            log.info(
                "fx_reval.zero",
                extra={"company_id": str(company_id), "currency": reval.currency},
            )
            continue

        existing = await _existing_reval_journal(
            session, company_id, reval.currency, through_date
        )
        if existing is not None:
            result.skipped_currencies.append(reval.currency)
            log.info(
                "fx_reval.skipped_idempotent",
                extra={
                    "company_id": str(company_id),
                    "currency": reval.currency,
                    "existing_entry_id": str(existing.id),
                },
            )
            continue

        adj_id, rev_id = await _post_reval_pair(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            reval=reval,
            through_date=through_date,
            ar_account=ar_account,
            ap_account=ap_account,
            gain_account=gain_account,
            loss_account=loss_account,
            posted_by=posted_by,
        )
        result.entries.append((adj_id, rev_id))
        log.info(
            "fx_reval.posted",
            extra={
                "company_id": str(company_id),
                "currency": reval.currency,
                "through_date": through_date.isoformat(),
                "ar_delta": str(reval.ar_delta),
                "ap_delta": str(reval.ap_delta),
                "adjustment_entry_id": str(adj_id),
                "reversal_entry_id": str(rev_id),
            },
        )

    return result


async def revalue_all_companies(
    session: AsyncSession,
    *,
    through_date: date,
    source: str = "rba",
    base_currency: str = "AUD",
    posted_by: str | None = None,
) -> dict[uuid.UUID, RevalResult]:
    """Convenience wrapper — iterate every active company.

    Used by the CLI's no-``--company-id`` path. Per-company errors are
    caught and logged so a bad rate on one tenant doesn't abort the
    whole cron run. The result dict only contains companies that ran
    cleanly; failed tenants are flagged in the log.
    """
    from saebooks.models.company import Company

    companies = (
        await session.execute(
            select(Company).where(Company.archived_at.is_(None))
        )
    ).scalars().all()

    out: dict[uuid.UUID, RevalResult] = {}
    for company in companies:
        try:
            out[company.id] = await revalue_company(
                session,
                company_id=company.id,
                tenant_id=company.tenant_id,
                through_date=through_date,
                source=source,
                base_currency=base_currency,
                posted_by=posted_by,
            )
        except Exception:
            log.exception(
                "fx_reval.company_failed",
                extra={"company_id": str(company.id)},
            )
    return out


__all__ = [
    "CurrencyReval",
    "FxRevalError",
    "RevalResult",
    "preview_company",
    "revalue_all_companies",
    "revalue_company",
]
