"""NZ tax engine — GST + GST101.

Implements the ``TaxEngine`` protocol for New Zealand. NZ GST is
similar in shape to AU GST (single national rate, output/input on
sales/purchases, periodic return) but differs in:

* Standard rate is **15%** (not 10%).
* Exports of goods/services are **zero-rated** (rate 0%, but the
  supply is reportable on the return AND input GST on associated
  costs is claimable).
* Financial services and residential rent are **exempt** (no GST
  charged, no input GST claimable).
* The return form is **GST101 / GST103** (not BAS); IRD labels are
  numeric box references (Box 5 / Box 6 / Box 8 / Box 11 / Box 13).

This module mirrors the layout of ``tax_engine.au``:

* ``GST101Line`` / ``GST101Report`` — dataclasses for the period
  summary (parallel to ``BASLine`` / ``BASReport``).
* ``gst101_report`` — async DB-driven period summary.
* ``NZTaxEngine`` — protocol-conforming class with sync ``compute``
  and ``boxes``.

Like AU, ``compute`` is sync and pure. The caller passes a
``PostingContext`` with the tax_code, rate, and reporting_type already
resolved; the engine snapshots the tax determination onto a
``TaxTreatment`` for storage on the journal line.
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
from saebooks.services.tax_engine.types import (
    PostingContext,
    TaxTreatment,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Account-type → tax-direction tables.
#
# Identical to the AU mapping — both jurisdictions carry GST as
# output on income, input on expense/asset. Kept module-local rather
# than imported from ``au`` so a future divergence (e.g. NZ-specific
# treatment of capital purchases) doesn't have to fight an AU import.
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

# Account types considered "income" / "purchases" for GST101 purposes.
_INCOME_TYPES = _OUTPUT_TYPES
_PURCHASE_TYPES = _INPUT_TYPES


# ---------------------------------------------------------------------------
# Canonical NZ reporting_type vocabulary.
#
# These are the strings the engine accepts on a ``PostingContext`` and
# the strings the period-summary aggregator buckets by. They form the
# contract between the tax_codes seed (NZ COA template, M1) and the
# engine; documented here so future seed work has a single reference.
#
#   "standard"   — taxable supply at the prevailing rate (15%). Goes
#                  to Box 5 + Box 8 (output) or Box 11 + Box 13 (input).
#   "zero_rated" — zero-rated supply (exports). Reported on the return
#                  but no GST collected. Output side hits Box 6;
#                  associated input GST is still claimable as Box 13.
#   "exempt"     — exempt supply (financial services, residential rent).
#                  Not reportable on the GST101 supply boxes; input
#                  GST on associated costs is NOT claimable.
#   "no_tax"     — out-of-scope / non-supply lines (equity, transfers,
#                  pre-engine legacy). No box, no claim.
#
# The engine does not enforce the vocabulary on ``compute`` — unknown
# values pass through unchanged so a typo is recoverable. The
# ``gst101_report`` aggregator only buckets the four canonical values
# and silently drops anything else (matching AU behaviour).
# ---------------------------------------------------------------------------

REPORTING_STANDARD = "standard"
REPORTING_ZERO_RATED = "zero_rated"
REPORTING_EXEMPT = "exempt"
REPORTING_NO_TAX = "no_tax"

# Default canonical tax-code strings — used as fall-throughs when the
# caller didn't supply ``tax_code`` on the context. The NZ COA template
# (M1) will define matching ``tax_codes`` rows so resolved codes line
# up with these strings.
CODE_STANDARD = "GST"
CODE_ZERO_RATED = "ZERO"
CODE_EXEMPT = "EXEMPT"

# Standard NZ GST rate as a decimal fraction. Stored as a fraction
# (``0.15``) rather than percentage points (``15.000``) to match the
# convention the AU engine uses internally; the actual rate value
# carried on a ``PostingContext`` is whatever the caller passes — the
# engine round-trips it as-is.
STANDARD_RATE = Decimal("0.15")


# ---------------------------------------------------------------------------
# GST101 report dataclasses.
#
# One dataclass per supply / purchase box we currently care about.
# Only the boxes that change with normal trading activity are modelled;
# adjustments (debit notes, credit notes, bad debts, secondhand goods,
# imported services) are intentionally out of scope until a real
# customer wires them up. The shape mirrors ``BASReport`` so reports
# routers can pick a renderer by jurisdiction without learning two
# protocols.
# ---------------------------------------------------------------------------


@dataclass
class GST101Line:
    label: str
    description: str
    amount: Decimal = Decimal("0")


@dataclass
class GST101Report:
    """Period summary for an NZ GST101 / GST103 return.

    Box numbers follow the IRD GST101A form. Amounts are in NZD,
    GST-inclusive on the supply side (mirroring AU's BAS conventions
    for G1/G10/G11) and GST-exclusive nowhere (NZ's form takes
    inclusive figures and derives GST in box 8/13).
    """

    period_from: date | None
    period_to: date | None
    box5: GST101Line   # Total sales and income (incl. GST and zero-rated)
    box6: GST101Line   # Zero-rated supplies
    box8: GST101Line   # Total GST collected on sales
    box11: GST101Line  # Total purchases and expenses (incl. GST)
    box13: GST101Line  # Total GST claimed on purchases

    @property
    def gst_payable(self) -> Decimal:
        """Net GST: collected minus claimed. Positive = owe IRD."""
        return self.box8.amount - self.box13.amount


# ---------------------------------------------------------------------------
# GST101 period summary — async DB-driven path.
#
# Mirrors ``tax_engine.au.bas_report``. Single SQL pass over the
# posted journal lines for the period, joined to ``accounts`` for
# direction and ``tax_codes`` for reporting_type.
# ---------------------------------------------------------------------------


async def gst101_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> GST101Report:
    """Build a GST101 report for the given period.

    Walks every posted journal line in the window, classifying by
    ``Account.account_type`` and ``TaxCode.reporting_type``:

    * Income side, ``standard`` → box5 (incl. GST), box8 (GST).
    * Income side, ``zero_rated`` → box5 + box6 (no GST collected).
    * Income side, ``exempt`` → not reported on the supply boxes.
    * Purchase side, ``standard`` → box11 (incl. GST), box13 (GST).
    * Purchase side, ``zero_rated`` → box11 (claimable GST is zero
      because the supply itself was zero-rated upstream — we still
      include the cost on box11 so the inclusive-purchases total
      reconciles).
    * Purchase side, ``exempt`` → not reported.
    """
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

    box5 = Decimal("0")
    box6 = Decimal("0")
    box8 = Decimal("0")
    box11 = Decimal("0")
    box13 = Decimal("0")

    for row in result.all():
        acct_type = row[0]
        reporting_type = row[1] or REPORTING_NO_TAX
        debit = row[2]
        credit = row[3]
        gst = row[4] or Decimal("0")

        if acct_type in _INCOME_TYPES:
            net = credit - debit
            if reporting_type == REPORTING_STANDARD:
                # Inclusive total (net + GST) goes on Box 5; GST on Box 8.
                box5 += net + gst
                box8 += gst
            elif reporting_type == REPORTING_ZERO_RATED:
                # Zero-rated supplies appear on Box 5 (total sales) AND
                # on Box 6 (the zero-rated subset). GST collected is zero.
                box5 += net
                box6 += net
            elif reporting_type == REPORTING_EXEMPT:
                # Not reportable on the supply side. Skip.
                pass
        elif acct_type in _PURCHASE_TYPES:
            net = debit - credit
            if reporting_type == REPORTING_STANDARD:
                box11 += net + gst
                box13 += gst
            elif reporting_type == REPORTING_ZERO_RATED:
                # Cost recognised, no claimable GST.
                box11 += net
            elif reporting_type == REPORTING_EXEMPT:
                # Not claimable, not reported on Box 11.
                pass

    return GST101Report(
        period_from=from_date,
        period_to=to_date,
        box5=GST101Line("Box 5", "Total sales and income (incl. GST)", box5),
        box6=GST101Line("Box 6", "Zero-rated supplies", box6),
        box8=GST101Line("Box 8", "Total GST collected on sales", box8),
        box11=GST101Line(
            "Box 11", "Total purchases and expenses (incl. GST)", box11
        ),
        box13=GST101Line("Box 13", "Total GST claimed on purchases", box13),
    )


# ---------------------------------------------------------------------------
# NZ TaxEngine — protocol-conforming class.
# ---------------------------------------------------------------------------


class NZTaxEngine:
    """New Zealand GST tax engine — implements the ``TaxEngine`` protocol.

    M1 deliverable: ``compute`` is sync and pure. The engine round-
    trips the caller-supplied rate and reporting_type, and derives
    ``base`` / ``tax`` from the line amount and rate (or trusts the
    pre-computed ``gst_amount`` if the caller already split it).

    ``boxes`` accepts a pre-built ``GST101Report`` (from
    ``gst101_report`` above) and returns the IRD box mapping for
    rendering.

    ``validate`` is a stub — pre-post validation lives in the calling
    service today (services/invoices.py) and is jurisdiction-agnostic.
    """

    jurisdiction: str = "NZ"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Determine the tax treatment for one journal line.

        Decision tree:

        * ``account_type`` in income types → ``direction='output'``.
        * ``account_type`` in expense / asset types →
          ``direction='input'``, BUT only if the line is *not* exempt
          — exempt purchases yield ``direction='none'`` because no
          input GST is claimable. (This matters for the period
          summary: an exempt expense should not be aggregated into
          Box 11 / Box 13 even though the account type would
          otherwise classify it as a purchase.)
        * Anything else → ``direction='none'``.

        ``rate`` is round-tripped from the input as-is. NZ callers
        should pass ``Decimal('0.15')`` for the standard rate, ``0``
        for zero-rated and exempt. The engine does not infer the
        rate from the reporting_type — keeping the determination
        deterministic and the caller in control means a forensic
        reconstruction can re-run the engine against historical
        ``tax_codes`` rows without the engine second-guessing them.

        ``gst_amount`` overrides rate-based derivation when the caller
        already knows it (mirrors AU). When neither is supplied the
        tax is zero — matching zero-rated / exempt lines.
        """
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or REPORTING_NO_TAX
        code = ctx.tax_code or CODE_STANDARD

        if ctx.account_type in _OUTPUT_TYPES:
            direction = "output"
        elif ctx.account_type in _INPUT_TYPES:
            # Exempt purchases: no input claim → direction is "none"
            # so downstream summary code can't accidentally aggregate
            # them into the purchases boxes.
            direction = (
                "none" if reporting_type == REPORTING_EXEMPT else "input"
            )
        else:
            direction = "none"

        # Derive base + tax. If the caller supplied gst_amount we
        # trust it; otherwise tax = base * rate. Caller's amount is
        # the GST-exclusive base for NZ (mirroring AU).
        base = ctx.amount
        if ctx.gst_amount is not None:
            tax = ctx.gst_amount
        elif rate and rate != Decimal("0"):
            tax = (base * rate).quantize(Decimal("0.01"))
        else:
            tax = Decimal("0")

        return TaxTreatment(
            jurisdiction="NZ",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=direction,
        )

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Return GST101 box labels for a period.

        Accepts either a pre-built ``GST101Report`` directly or a
        duck-typed object with a ``.report`` attribute (for symmetry
        with the AU engine). The async ``gst101_report`` helper above
        is the production path; this method is the protocol-uniform
        entry point.
        """
        report: GST101Report
        if isinstance(period, GST101Report):
            report = period
        elif hasattr(period, "report") and isinstance(
            period.report, GST101Report
        ):
            report = period.report
        else:
            raise NotImplementedError(
                "NZTaxEngine.boxes requires a pre-built GST101Report for now. "
                "Use saebooks.services.tax_engine.nz.gst101_report(...) to "
                "produce one and pass it in."
            )

        return {
            "Box 5": report.box5.amount,
            "Box 6": report.box6.amount,
            "Box 8": report.box8.amount,
            "Box 11": report.box11.amount,
            "Box 13": report.box13.amount,
        }

    def validate(self, invoice: Any) -> list[ValidationError]:
        """NZ pre-post checks. Stub for now — every NZ validation will
        live in the calling service when invoice posting on NZ
        companies is wired up; this method exists to satisfy the
        protocol."""
        return []


__all__ = [
    "CODE_EXEMPT",
    "CODE_STANDARD",
    "CODE_ZERO_RATED",
    "REPORTING_EXEMPT",
    "REPORTING_NO_TAX",
    "REPORTING_STANDARD",
    "REPORTING_ZERO_RATED",
    "STANDARD_RATE",
    "GST101Line",
    "GST101Report",
    "NZTaxEngine",
    "gst101_report",
]
