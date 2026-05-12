"""AU tax engine — GST + BAS.

Reference implementation. Moved here from ``services/gst.py`` (auto-
posting GST Collected / GST Paid lines on journal post) and
``services/bas.py`` (G1/G2/G3/G10/G11/1A/1B period summary).

The two old modules remain as thin re-export shims for one release —
existing callers continue to import ``services.gst`` /
``services.bas`` and get a ``DeprecationWarning`` at import. The
shims are dropped at M1 entry once internal callers move over.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import settings as settings_svc
from saebooks.services.tax_engine.types import (
    PostingContext,
    TaxTreatment,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Account-type → tax-direction tables (shared between auto-post and
# the engine's ``compute``).
# ---------------------------------------------------------------------------

_INPUT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    AccountType.ASSET,
})
_OUTPUT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
})

# Account types considered "income" for BAS purposes.
_BAS_INCOME_TYPES = _OUTPUT_TYPES

# Account types considered "purchases" for BAS purposes.
_BAS_PURCHASE_TYPES = _INPUT_TYPES


# ---------------------------------------------------------------------------
# BAS report dataclasses (kept stable for callers re-importing from
# ``services.bas``).
# ---------------------------------------------------------------------------


@dataclass
class BASLine:
    label: str
    description: str
    amount: Decimal = Decimal("0")


@dataclass
class BASReport:
    period_from: date | None
    period_to: date | None
    g1: BASLine
    g2: BASLine
    g3: BASLine
    g10: BASLine
    g11: BASLine
    label_1a: BASLine
    label_1b: BASLine

    @property
    def gst_payable(self) -> Decimal:
        """Net GST: collected minus paid. Positive = owe ATO."""
        return self.label_1a.amount - self.label_1b.amount


# ---------------------------------------------------------------------------
# GST auto-post helpers — internal to the engine; exported for the
# ``services.gst`` shim.
# ---------------------------------------------------------------------------


async def is_auto_post_enabled(session: AsyncSession) -> bool:
    val = await settings_svc.get(session, "gst_auto_post", "true")
    return str(val).lower() in ("true", "1", "yes")


async def _get_gst_account(
    session: AsyncSession, company_id: uuid.UUID, setting_key: str
) -> Account | None:
    raw = await settings_svc.get(session, setting_key, "")
    if not raw:
        return None
    code = str(raw)
    if "-" not in code and len(code) >= 2 and code[0].isdigit():
        hyphenated = code[0] + "-" + code[1:]
    else:
        hyphenated = code
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_([code, hyphenated]),
            Account.archived_at.is_(None),
        )
    )
    return result.scalars().first()


async def auto_post_gst_lines(
    session: AsyncSession,
    entry: JournalEntry,
) -> list[JournalLine]:
    """Generate GST account lines for a journal entry being posted.

    For each line that has a gst_amount, creates a corresponding line
    on the appropriate GST account (Collected or Paid). Returns the
    list of new GST lines added — the caller flushes/commits.
    """
    if not await is_auto_post_enabled(session):
        return []

    collected_acct = await _get_gst_account(
        session, entry.company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, entry.company_id, "gst_paid_account_code"
    )

    if not collected_acct or not paid_acct:
        return []

    acct_ids = {line.account_id for line in entry.lines}
    acct_types: dict[uuid.UUID, AccountType] = {}
    if acct_ids:
        result = await session.execute(
            select(Account.id, Account.account_type).where(Account.id.in_(acct_ids))
        )
        for row in result.all():
            acct_types[row[0]] = row[1]

    new_lines: list[JournalLine] = []
    max_line_no = max((line.line_no for line in entry.lines), default=0)

    for line in entry.lines:
        gst = line.gst_amount
        if not gst or gst == Decimal("0"):
            continue
        if line.account_id in (collected_acct.id, paid_acct.id):
            continue
        acct_type = acct_types.get(line.account_id)
        if acct_type is None:
            continue

        max_line_no += 1
        if acct_type in _OUTPUT_TYPES:
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=collected_acct.id,
                description=f"GST on {line.description or 'sale'}",
                debit=Decimal("0"),
                credit=abs(gst),
            )
        elif acct_type in _INPUT_TYPES:
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=paid_acct.id,
                description=f"GST on {line.description or 'purchase'}",
                debit=abs(gst),
                credit=Decimal("0"),
            )
        else:
            continue

        session.add(gst_line)
        entry.lines.append(gst_line)
        new_lines.append(gst_line)

    return new_lines


async def settle_bas(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    settlement_date: date,
    from_date: date | None = None,
    to_date: date | None = None,
) -> JournalEntry | None:
    """Create a draft BAS settlement journal entry."""
    from saebooks.services import journal as journal_svc

    collected_acct = await _get_gst_account(
        session, company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, company_id, "gst_paid_account_code"
    )
    clearing_acct = await _get_gst_account(
        session, company_id, "gst_clearing_account_code"
    )

    if not collected_acct or not paid_acct or not clearing_acct:
        return None

    from saebooks.services.reports import _account_balances

    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=to_date
    )

    collected_bal = Decimal("0")
    paid_bal = Decimal("0")

    for bal in balances:
        if bal.account_id == collected_acct.id:
            collected_bal = bal.balance
        elif bal.account_id == paid_acct.id:
            paid_bal = bal.balance

    if collected_bal == Decimal("0") and paid_bal == Decimal("0"):
        return None

    lines: list[dict[str, object]] = []

    if collected_bal != Decimal("0"):
        lines.append({
            "account_id": collected_acct.id,
            "description": "Clear GST Collected for BAS",
            "debit": abs(collected_bal),
            "credit": Decimal("0"),
        })

    if paid_bal != Decimal("0"):
        lines.append({
            "account_id": paid_acct.id,
            "description": "Clear GST Paid for BAS",
            "debit": Decimal("0"),
            "credit": abs(paid_bal),
        })

    net = abs(collected_bal) - paid_bal
    if net > Decimal("0"):
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST payable to ATO",
            "debit": Decimal("0"),
            "credit": net,
        })
    elif net < Decimal("0"):
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST refund from ATO",
            "debit": abs(net),
            "credit": Decimal("0"),
        })

    if not lines:
        return None

    period_label = ""
    if from_date and to_date:
        period_label = f" ({from_date} to {to_date})"

    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        entry_date=settlement_date,
        description=f"BAS settlement{period_label}",
        lines=lines,
    )

    return entry


async def bas_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> BASReport:
    """Build a BAS report for the given period."""
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status == EntryStatus.POSTED,
    ]
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date:
        conditions.append(JournalEntry.entry_date <= to_date)

    stmt = (
        select(
            Account.account_type,
            TaxCode.reporting_type,
            JournalLine.debit,
            JournalLine.credit,
            JournalLine.gst_amount,
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .outerjoin(TaxCode, JournalLine.tax_code_id == TaxCode.id)
        .where(and_(*conditions))
    )
    result = await session.execute(stmt)

    g1 = Decimal("0")
    g2 = Decimal("0")
    g3 = Decimal("0")
    g10 = Decimal("0")
    g11 = Decimal("0")
    gst_collected = Decimal("0")
    gst_paid = Decimal("0")

    for row in result.all():
        acct_type = row[0]
        reporting_type = row[1] or "no_tax"
        debit = row[2]
        credit = row[3]
        gst = row[4] or Decimal("0")

        if acct_type in _BAS_INCOME_TYPES:
            net = credit - debit
            if reporting_type == "taxable":
                g1 += net + gst
                gst_collected += gst
            elif reporting_type == "export":
                g2 += net
            elif reporting_type == "gst_free":
                g3 += net
        elif acct_type in _BAS_PURCHASE_TYPES:
            net = debit - credit
            if reporting_type in ("taxable", "capital"):
                if reporting_type == "capital":
                    g10 += net + gst
                else:
                    g11 += net + gst
                gst_paid += gst
            elif reporting_type == "gst_free":
                pass

    return BASReport(
        period_from=from_date,
        period_to=to_date,
        g1=BASLine("G1", "Total sales (including any GST)", g1),
        g2=BASLine("G2", "Export sales", g2),
        g3=BASLine("G3", "Other GST-free sales", g3),
        g10=BASLine("G10", "Capital purchases (including any GST)", g10),
        g11=BASLine("G11", "Non-capital purchases (including any GST)", g11),
        label_1a=BASLine("1A", "GST collected on sales", gst_collected),
        label_1b=BASLine("1B", "GST paid on purchases", gst_paid),
    )


# ---------------------------------------------------------------------------
# AU TaxEngine — protocol-conforming class.
# ---------------------------------------------------------------------------


class AUTaxEngine:
    """Australia GST tax engine — implements the ``TaxEngine`` protocol.

    M0: ``compute`` is sync and pure (no DB). It uses the
    ``PostingContext`` fields the caller already filled in (rate,
    reporting_type, gst_amount) — the engine doesn't look up tax_code
    rows itself; that's the caller's job. This keeps the engine
    cheap to call inline from journal-line construction.

    ``boxes`` and ``validate`` keep their AU shape but are stubs at
    this layer — the existing async ``bas_report`` helper above is
    the AU period-summary path used by the reports router. ``boxes``
    here is a sync convenience that returns the BAS report shape
    given a pre-built ``BASReport`` (passed in via ``period.report``)
    so the protocol is uniform without forcing the engine to issue
    DB calls.
    """

    jurisdiction: str = "AU"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        # ``rate`` is round-tripped from the input as-is. Production
        # callers fill this from ``TaxCode.rate`` which stores the rate
        # in percentage points (``10.000`` == 10%). Direct unit-test
        # callers may pass a fraction (``0.10``) — both are valid; the
        # engine stores what it's given. Tax derivation falls back to
        # ``gst_amount`` when supplied so the convention only matters
        # for tax_engine consumers reading the snapshot back.
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "GST"

        # Direction: sales (credit-normal income) → output; purchases
        # (debit-normal expense / asset) → input. Anything else is a
        # no-tax line.
        if ctx.account_type in _OUTPUT_TYPES:
            direction = "output"
        elif ctx.account_type in _INPUT_TYPES:
            direction = "input"
        else:
            direction = "none"

        # Derive base + tax. If the caller supplied gst_amount we
        # trust it; otherwise we compute base = amount, tax = base * rate
        # (the caller's amount is already the net for AU GST lines).
        base = ctx.amount
        if ctx.gst_amount is not None:
            tax = ctx.gst_amount
        elif rate and rate != Decimal("0"):
            tax = (base * rate).quantize(Decimal("0.01"))
        else:
            tax = Decimal("0")

        return TaxTreatment(
            jurisdiction="AU",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=direction,
        )

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Return BAS labels for a period.

        Accepts either a pre-built ``BASReport`` (passed via
        ``period.report``) or any duck-typed object with the seven
        BAS attributes. The async DB-driven ``bas_report`` helper
        above is the production path; this method is the protocol-
        uniform entry point.
        """
        report: BASReport
        if isinstance(period, BASReport):
            report = period
        elif hasattr(period, "report") and isinstance(period.report, BASReport):
            report = period.report
        else:
            raise NotImplementedError(
                "AUTaxEngine.boxes requires a pre-built BASReport for now. "
                "Use saebooks.services.tax_engine.au.bas_report(...) to "
                "produce one and pass it in."
            )

        return {
            "G1": report.g1.amount,
            "G2": report.g2.amount,
            "G3": report.g3.amount,
            "G10": report.g10.amount,
            "G11": report.g11.amount,
            "1A": report.label_1a.amount,
            "1B": report.label_1b.amount,
        }

    def validate(self, invoice: Any) -> list[ValidationError]:
        """AU pre-post checks. Stub at M0 — every AU validation today
        lives in the calling service (services/invoices.py); this
        method exists to satisfy the protocol and is the hook NZ/UK
        will use."""
        return []
