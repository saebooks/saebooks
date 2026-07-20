"""UK tax engine — VAT, including the reverse-charge / postponed-VAT
two-component fan-out, and the VAT100 period summary.

UK jurisdiction module (bolt-on architecture; AU ``jurisdictions/au/
tax.py`` is the reference implementation, EE ``tax_engine/ee.py`` the
reverse-charge precedent).

Ordinary UK VAT lines behave identically to the AU/EE algorithm:
direction from account_type (``_INPUT_TYPES``/``_OUTPUT_TYPES``,
imported from the AU module rather than duplicated — the mapping is
jurisdiction-neutral GL-account semantics, not a VAT rule), base = the
line amount, tax = ``gst_amount`` when the caller supplies it, else
``base * rate / 100``.

Reverse charge / PVA (the EE ``rc_eu_acq_*`` precedent): a purchase
line whose company-side ``TaxCode.reporting_type`` is in
``RC_DUAL_REPORTING_TYPES`` self-assesses output VAT AND reclaims the
same amount as input VAT — one journal line emits BOTH an output-role
component and an input-role component. Box routing is data-driven from
``seeds/jurisdictions/UK/tax_return_box_definitions.yaml``:

* ``rc_construction`` (domestic reverse charge, construction),
  ``rc_services_intl`` (services received from abroad) and
  ``pva_import`` (postponed import VAT accounting) — output component
  feeds VAT100 box 1 (internal box ``1_RC``), input component feeds
  box 4 (``4_RC``).
* ``xi_eu_acq_goods`` (Northern Ireland protocol EU acquisitions) —
  output component feeds box 2, input component feeds box 4.

Unlike EE's KMD (whose output boxes are rate-split, forcing
``EETaxEngine`` to reject rates with no wired box), VAT100's output
boxes are NOT rate-bucketed — the role-keyed internal boxes carry no
``@<rate>`` pin, so ANY rate routes correctly and this engine needs no
supported-rate refusal. Partial recovery: ``ctx.extra
["deductible_fraction"]`` (default ``1``) scales ONLY the input-role
component — the self-assessed output liability is never partial (the
EE convention; relevant in the UK for partial exemption, see
``seeds/jurisdictions/UK/vat_schemes.yaml``).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
# UK VAT100 seed's role-keyed boxes (1_RC / 2 / 4_RC) read. The
# seller-side ``rc_construction_supply`` tag is a plain zero-rated sale
# (box 6 only) and is deliberately NOT here — the party that
# self-assesses under the construction DRC is the CUSTOMER, on their
# own ledger.
RC_DUAL_REPORTING_TYPES: frozenset[str] = frozenset({
    "rc_construction",
    "rc_services_intl",
    "pva_import",
    "xi_eu_acq_goods",
})

_TWO_PLACES = money_quantum(2)


class UKTaxEngine:
    """United Kingdom VAT tax engine — implements the ``TaxEngine``
    protocol (see ``services.tax_engine.__init__``)."""

    jurisdiction: str = "UK"

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
        code = ctx.tax_code or "VAT"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        return TaxTreatment(
            jurisdiction="UK",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=self._direction(ctx),
        )

    def _compute_reverse_charge(self, ctx: PostingContext) -> list[TaxTreatment]:
        """One reverse-charge/PVA purchase line emits BOTH an
        output-role component (self-assessed VAT due — VAT100 box 1, or
        box 2 for NI-protocol EU acquisitions) and an input-role
        component (the reclaim — box 4).

        Both components share the same ``base`` (the net value — a
        reverse charge does not change the taxable base, only who
        accounts for the VAT) and, for full recovery, the same ``tax``.
        ``ctx.extra["deductible_fraction"]`` (default ``1``) scales
        ONLY the input-role component — the output liability is never
        partial; only the input-credit entitlement can be capped
        (partial exemption).
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
            jurisdiction="UK",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction="output",
            notes=("reverse_charge_output",),
        )
        input_component = TaxTreatment(
            jurisdiction="UK",
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
        """Not used — VAT100 period summaries go through
        ``tax_return_generator.generate_return(jurisdiction="UK",
        return_type="VAT100")`` (this module's :func:`vat100_report`
        convenience), mirroring the AU/EE note that the data-driven
        generator — not this protocol method — is the reporting path."""
        raise NotImplementedError(
            "UKTaxEngine.boxes is not used — call "
            "saebooks.jurisdictions.uk.tax.vat100_report(...) or "
            "saebooks.services.tax_return_generator.generate_return("
            "jurisdiction='UK', return_type='VAT100', ...) instead."
        )

    def validate(self, invoice: Any) -> list[ValidationError]:
        """UK pre-post checks. Stub at module bring-up — satisfies the
        protocol; the hook MTD-side validation will use."""
        return []


async def vat100_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> Any:
    """Build the 9-box VAT100 for a period — a thin wrapper over the
    generic, data-driven return calculator (the AU ``bas_report``
    parallel). Box recipes come from the GBR/VAT100 rows of
    ``tax_return_box_definitions`` (reference DB when configured; the
    seed YAML is their source of truth). Returns the generator's
    ``TaxReturnResult`` — box_code -> box result including the internal
    ``*_DOMESTIC``/``*_RC`` ledger legs (display_order >= 100), which
    callers presenting the statutory 9 boxes should filter out.
    Local import to avoid a module-load cycle (the generator imports
    the AU account-type sets at import time — same note as
    ``jurisdictions/au/tax.py::bas_report``)."""
    from saebooks.services.tax_return_generator import generate_return

    return await generate_return(
        session,
        company_id,
        jurisdiction="UK",
        return_type="VAT100",
        from_date=from_date,
        to_date=to_date,
    )
