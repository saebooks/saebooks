"""NZ tax engine — GST + GST101A.

Jurisdiction-module bolt-on (design doc §2): the NZ ``TaxEngine``
implementation, a sibling of ``jurisdictions.au.tax.AUTaxEngine`` and
``services.tax_engine.ee.EETaxEngine``. Reached via the lazy
``_nz_factory`` in ``services.tax_engine`` (same in-file wiring shape
as AU's Phase-2 factory) so ``journal._apply_tax_treatment`` dispatches
here for any ``Company.jurisdiction == "NZ"`` post.

Determination model
-------------------

NZ GST is a single-rate (15%), no-reduced-rate regime, so ``compute``
is the plain single-component algorithm every engine shares: direction
from the posting account's type (the jurisdiction-neutral GL semantics
imported from ``jurisdictions.au.tax`` — the same reuse ``ee.py``
makes), base = the line amount, tax = the caller-supplied
``gst_amount`` when present, else ``base * rate / 100`` (rate in
percentage points, the EE convention — NOT AU's historical
rate-as-fraction fallback). No multi-component fan-out exists in NZ GST
(no reverse-charge acquisition regime in scope), so
``compute_components`` is the trivial single-element list.

The s 10(6) long-stay commercial accommodation rule ("9% effective") is
a VALUE-APPORTIONMENT rule, not a rate: the taxable value is 60% of the
consideration, taxed at the ordinary 15%. The engine applies no special
case — the caller supplies the apportioned base (or the pre-computed
``gst_amount``), per the ACCOM_LT seed row's usage note
(``seeds/jurisdictions/NZ/tax_codes.yaml``).

GST101A reporting
-----------------

``gst101_report`` is the thin data-driven wrapper over
``tax_return_generator.generate_return`` (the same shape as
``jurisdictions.au.tax.bas_report``): box definitions live in
``seeds/jurisdictions/NZ/tax_return_box_definitions.yaml`` (boxes 5-15,
3/23 extraction, signed Box 15) and are read from the reference DB.
There is deliberately NO embedded fallback box set for NZ (same
reference-DB-only posture as EE KMD) — NZ GST101 reporting requires the
seeded reference DB; core AU reporting is unaffected either way.

GST103B (GST + provisional tax combined) is NOT implemented — its
box-level detail is unverified (parked; see the NZ seed header).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.jurisdictions.nz import (
    identifiers as _identifiers,  # noqa: F401  (registers the nz_nzbn validator on first NZ dispatch)
)
from saebooks.money import money_quantum
from saebooks.services.tax_engine.types import (
    INPUT_ACCOUNT_TYPES as _INPUT_TYPES,
)
from saebooks.services.tax_engine.types import (
    OUTPUT_ACCOUNT_TYPES as _OUTPUT_TYPES,
)
from saebooks.services.tax_engine.types import (
    PostingContext,
    TaxTreatment,
    ValidationError,
)

_TWO_PLACES = money_quantum(2)

#: The GST101A return_type key in ``tax_return_box_definitions``
#: (seeds/jurisdictions/NZ/tax_return_box_definitions.yaml).
GST101_RETURN_TYPE = "GST101"


class NZTaxEngine:
    """New Zealand GST tax engine — implements the ``TaxEngine``
    protocol (see ``services.tax_engine.__init__``)."""

    jurisdiction: str = "NZ"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "GST"
        base = ctx.amount

        if ctx.account_type in _OUTPUT_TYPES:
            direction = "output"
        elif ctx.account_type in _INPUT_TYPES:
            direction = "input"
        else:
            direction = "none"

        if ctx.gst_amount is not None:
            tax = ctx.gst_amount
        elif rate and rate != Decimal("0"):
            # Rate is percentage points (TaxCode.rate convention: 15.000
            # == 15%) — divide by 100, the EE convention.
            tax = (base * rate / Decimal("100")).quantize(_TWO_PLACES)
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

    def compute_components(self, ctx: PostingContext) -> list[TaxTreatment]:
        """Single-component always — NZ GST has no reverse-charge
        fan-out or stacked-tax need; the dispatcher materialises exactly
        one ``JournalLineTaxComponent`` row per line, same as AU."""
        return [self.compute(ctx)]

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Not used — GST101A period summaries go through
        ``gst101_report`` (data-driven ``generate_return``), mirroring
        EE's convention that the async reference-DB path, not this sync
        protocol method, is the production reporting entry point."""
        raise NotImplementedError(
            "NZTaxEngine.boxes is not used — call "
            "saebooks.jurisdictions.nz.tax.gst101_report(...) (which "
            "wraps tax_return_generator.generate_return("
            "jurisdiction='NZ', return_type='GST101', ...)) instead."
        )

    def validate(self, invoice: Any) -> list[ValidationError]:
        """NZ pre-post checks. None yet — satisfies the protocol."""
        return []


async def gst101_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    manual_values: dict[str, Decimal] | None = None,
) -> Any:
    """Build a GST101A return for the period — thin wrapper over the
    data-driven return calculator (the exact shape of
    ``jurisdictions.au.tax.bas_report``).

    ``manual_values`` injects the two filer-entered calculation-sheet
    boxes (Box 9 output-tax adjustments, Box 13 credit adjustments —
    both ``manual`` in the seed); absent values are an explicit 0 in the
    Box 10/14/15 formulas. Returns the generator's ``TaxReturnResult``
    (boxes keyed "5".."15" plus the internal display_order>=100 legs,
    which ``persist_return`` excludes from persisted figures).

    Local import to avoid a module-load cycle: ``tax_return_generator``
    imports this package's account-type sets (via ``au.tax``) at import
    time — same call-time-import rationale as ``bas_report``.
    """
    from saebooks.services.tax_return_generator import generate_return

    return await generate_return(
        session,
        company_id,
        jurisdiction="NZ",
        return_type=GST101_RETURN_TYPE,
        from_date=from_date,
        to_date=to_date,
        manual_values=manual_values,
    )


__all__ = ["GST101_RETURN_TYPE", "NZTaxEngine", "gst101_report"]
