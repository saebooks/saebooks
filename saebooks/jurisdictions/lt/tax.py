"""LT tax engine — PVM (VAT), including the reverse-charge
two-component fan-out, and the FR0600 period summary.

LT jurisdiction module (bolt-on architecture; AU ``jurisdictions/au/
tax.py`` is the reference implementation, EE ``tax_engine/ee.py`` and
UK ``jurisdictions/uk/tax.py`` the reverse-charge precedents).

Ordinary LT PVM lines behave identically to the AU/EE/NZ/UK algorithm:
direction from account_type (``_INPUT_TYPES``/``_OUTPUT_TYPES``,
imported from the AU module rather than duplicated — the mapping is
jurisdiction-neutral GL-account semantics, not a VAT rule), base = the
line amount, tax = ``gst_amount`` when the caller supplies it, else
``base * rate / 100`` (rate in percentage points).

Reverse charge (the EE ``rc_eu_acq_*`` precedent): a purchase line
whose company-side ``TaxCode.reporting_type`` is in
``RC_DUAL_REPORTING_TYPES`` self-assesses output VAT AND reclaims the
same amount as input VAT — one journal line emits BOTH an output-role
component and an input-role component. Box routing is data-driven from
``seeds/jurisdictions/LT/tax_return_box_definitions.yaml``:

* ``rc_eu_acq_goods`` (intra-EU acquisitions of goods) — output
  component feeds FR0600 box 34, input component feeds box 25 (via the
  internal ``25_RC``) and therefore box 35; the line's net feeds
  box 21 through the ordinary purchase bucket.
* ``rc_eu_acq_services`` / ``rc_services_noneu`` (Art 95 services from
  abroad) — output feeds box 32; input feeds 25_RC/35; net feeds
  box 23 (+ 24 for the EU variant).
* ``rc_domestic_acq`` (Art 96 domestic reverse charge, buyer side —
  construction works, metal waste/scrap per Gov. Resolution No 900) —
  output feeds box 33; input feeds 25_RC/35.

Unlike EE's KMD (whose output boxes are rate-split, forcing
``EETaxEngine`` to reject rates with no wired box), FR0600's
self-assessment boxes 32/33/34 are role-keyed and NOT rate-bucketed —
so ANY rate routes correctly and this engine needs no supported-rate
refusal (the UK VAT100 posture). Partial recovery:
``ctx.extra["deductible_fraction"]`` (default ``1``) scales ONLY the
input-role component — the self-assessed output liability is never
partial (the EE convention; relevant in LT for the Art 60 pro-rata,
whose box-28 return-level scaling is separately parked — see the box
seed's box-28 comment).

The seller side of the Art 96 domestic reverse charge
(``rc_domestic_supply``) is a plain zero-VAT sale feeding box 12 only
and is deliberately NOT in the fan-out set — the party that
self-assesses is the CUSTOMER, on their own ledger (the UK
``rc_construction_supply`` / EE ``rc_domestic_supply`` convention).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.jurisdictions.lt import (
    identifiers as _identifiers,  # noqa: F401  (registers the lt_* validators on first LT dispatch)
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

# Company-side TaxCode.reporting_type tags that trigger the two-
# component (output + input) reverse-charge fan-out — the SAME tags the
# LT FR0600 seed's role-keyed boxes (32 / 33 / 34 / 25_RC) read.
RC_DUAL_REPORTING_TYPES: frozenset[str] = frozenset({
    "rc_eu_acq_goods",
    "rc_eu_acq_services",
    "rc_services_noneu",
    "rc_domestic_acq",
})

_TWO_PLACES = money_quantum(2)

#: The FR0600 return_type key in ``tax_return_box_definitions``
#: (seeds/jurisdictions/LT/tax_return_box_definitions.yaml).
FR0600_RETURN_TYPE = "FR0600"


class LTTaxEngine:
    """Lithuania PVM (VAT) tax engine — implements the ``TaxEngine``
    protocol (see ``services.tax_engine.__init__``)."""

    jurisdiction: str = "LT"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Single-treatment entry point — the FIRST element
        ``compute_components`` returns (for a reverse-charge line that
        is the output-role component, the same convention
        ``services.journal._apply_tax_treatment`` snapshots)."""
        return self.compute_components(ctx)[0]

    def compute_components(self, ctx: PostingContext) -> list[TaxTreatment]:
        reporting_type = ctx.reporting_type or "no_tax"
        if reporting_type in RC_DUAL_REPORTING_TYPES:
            return self._compute_reverse_charge(ctx)
        return [self._compute_standard(ctx)]

    def _direction(self, ctx: PostingContext) -> str:
        if ctx.account_type in _OUTPUT_TYPES:
            return "output"
        if ctx.account_type in _INPUT_TYPES:
            return "input"
        return "none"

    def _derive_tax(
        self, ctx: PostingContext, *, rate: Decimal, base: Decimal
    ) -> Decimal:
        if ctx.gst_amount is not None:
            return ctx.gst_amount
        if rate and rate != Decimal("0"):
            return (base * rate / Decimal("100")).quantize(_TWO_PLACES)
        return Decimal("0")

    def _compute_standard(self, ctx: PostingContext) -> TaxTreatment:
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "PVM"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        return TaxTreatment(
            jurisdiction="LT",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=self._direction(ctx),
        )

    def _compute_reverse_charge(self, ctx: PostingContext) -> list[TaxTreatment]:
        """One reverse-charge purchase line emits BOTH an output-role
        component (self-assessed VAT due — FR0600 box 32/33/34 by tag)
        and an input-role component (the reclaim — box 25/35).

        Both components share the same ``base`` (the net value — a
        reverse charge does not change the taxable base, only who
        accounts for the VAT) and, for full recovery, the same ``tax``.
        ``ctx.extra["deductible_fraction"]`` (default ``1``) scales
        ONLY the input-role component — the output liability is never
        partial; only the input-credit entitlement can be capped
        (Art 60 pro-rata / non-deductible categories).
        """
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "RC"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        deductible_fraction = Decimal("1")
        if ctx.extra and "deductible_fraction" in ctx.extra:
            deductible_fraction = Decimal(str(ctx.extra["deductible_fraction"]))
        input_tax = (tax * deductible_fraction).quantize(_TWO_PLACES)

        output = TaxTreatment(
            jurisdiction="LT",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction="output",
            notes=("reverse_charge_output",),
        )
        input_component = TaxTreatment(
            jurisdiction="LT",
            code=code,
            rate=rate,
            base=base,
            tax=input_tax,
            reporting_type=reporting_type,
            direction="input",
            notes=("reverse_charge_input",),
        )
        return [output, input_component]

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Not used — FR0600 period summaries go through
        ``fr0600_report`` (data-driven ``generate_return``), mirroring
        the AU/EE/NZ/UK convention that the async reference-DB path,
        not this sync protocol method, is the reporting entry point."""
        raise NotImplementedError(
            "LTTaxEngine.boxes is not used — call "
            "saebooks.jurisdictions.lt.tax.fr0600_report(...) (which "
            "wraps tax_return_generator.generate_return("
            "jurisdiction='LT', return_type='FR0600', ...)) instead."
        )

    def validate(self, invoice: Any) -> list[ValidationError]:
        """LT pre-post checks. None yet — satisfies the protocol."""
        return []


async def fr0600_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    manual_values: dict[str, Decimal] | None = None,
) -> Any:
    """Build the FR0600 PVM return for a period — a thin wrapper over
    the generic, data-driven return calculator (the AU ``bas_report`` /
    UK ``vat100_report`` parallel). Box recipes come from the
    LTU/FR0600 rows of ``tax_return_box_definitions`` (reference DB
    when configured; the seed YAML is their source of truth).

    ``manual_values`` injects the filer-entered boxes (14/15/16/22/27/
    28 — all ``manual`` in the seed); an absent box 27 is an explicit 0
    in the box 35/36 formulas (the EE KMD box 4-1 precedent — and box
    27's two formula legs cancel, so box 36 is correct either way).
    Returns the generator's ``TaxReturnResult`` — box_code -> result
    including the internal ``25_DOMESTIC``/``25_RC`` ledger legs
    (display_order >= 100), which callers presenting the statutory
    boxes should filter out.

    Local import to avoid a module-load cycle (the generator imports
    the AU account-type sets at import time — same note as
    ``jurisdictions/au/tax.py::bas_report``).
    """
    from saebooks.services.tax_return_generator import generate_return

    return await generate_return(
        session,
        company_id,
        jurisdiction="LT",
        return_type=FR0600_RETURN_TYPE,
        from_date=from_date,
        to_date=to_date,
        manual_values=manual_values,
    )


__all__ = ["FR0600_RETURN_TYPE", "LTTaxEngine", "fr0600_report"]
