"""LV tax engine — pievienotās vērtības nodoklis (PVN) + the PVN
declaration, plus the distributed-profits UIN arithmetic helper.

Jurisdiction-module bolt-on (design doc §2): the LV ``TaxEngine``
implementation, a sibling of ``jurisdictions.au.tax.AUTaxEngine``,
``services.tax_engine.ee.EETaxEngine`` and the NZ/UK engines. Reached
via the lazy ``_lv_factory`` in ``services.tax_engine`` (the AU/NZ/UK
in-file wiring shape) so ``journal._apply_tax_treatment`` dispatches
here for any ``Company.jurisdiction == "LV"`` post.

Determination model
-------------------

Ordinary lines use the shared single-component algorithm (direction
from the posting account's type — the jurisdiction-neutral GL semantics
imported from ``jurisdictions.au.tax``, the same reuse every non-AU
engine makes; base = the line amount; tax = the caller-supplied
``gst_amount`` when present, else ``base * rate / 100`` with rate in
percentage points).

Reverse charge — LV routes DIFFERENTLY from EE; do not copy KMD:

* ``rc_eu_acq_goods`` / ``rc_eu_acq_services`` — EU acquisitions of
  goods / services from EU-registered suppliers. Two-component fan-out:
  the OUTPUT-role component's base feeds the dedicated declaration rows
  50/51/51.1 (by rate; VAT via the 55/56/56.1 rate formulas), the
  INPUT-role component's tax feeds row 64. Supported rates: 21/12/5
  (the three current PVN rates — each has a declaration row wired). Any
  other rate raises :class:`ReverseChargeRateNotSupportedError` — an
  unwired rate would land the base in NO output row while row 64 still
  deducted the input VAT, the same silent asymmetry the EE engine
  refuses.
* ``rc_third_country_services`` — services from third-country/
  third-territory suppliers (PVN likums Arts. 19(1)/20(1)/25).
  Two-component fan-out: output-role VAT feeds row 54 (a tax-amount
  row — the form declares no base row for these), input-role VAT feeds
  row 63. Same 21/12/5 rate gate for consistency.
* ``rc_domestic_acq`` — BUYER-side domestic reverse charge
  (Arts. 141-143.4). REFUSED (:class:`DomesticReverseChargeNotSupported
  Error`): the deductible leg is primary-verified to row 62 but the
  output leg's row was NOT primary-verified in the research pass —
  refusing loudly beats guessing a declaration row (the slice boundary
  stated, not buried). Seller-side domestic RC (``rc_domestic_supply``,
  row 41.1) is an ordinary single-component sale line and works.

``ctx.extra["deductible_fraction"]`` scales only the input-role
component (default 1 — full deduction), the EE §30 pattern; Latvia's
partial-deduction proportion (Art. 98) rides the same hook when a
caller starts passing a fraction.

PVN declaration reporting
-------------------------

``pvn_report`` is the thin data-driven wrapper over
``tax_return_generator.generate_return`` (the ``bas_report``/
``gst101_report``/``vat100_report`` shape): row recipes live in
``seeds/jurisdictions/LV/tax_return_box_definitions.yaml`` and are read
from the reference DB. Deliberately NO embedded fallback row set (the
EE/NZ/UK reference-DB-only posture).

UIN (corporate income tax) helper
---------------------------------

Latvia's corporate tax is the Estonian distributed-profits model with
different coefficients, plus a 2026 elective regime. The engine has no
core corporate-tax posting surface (EE's CIT likewise lives in its TSD
lodgement generator, not core) — so the module owns the pure
arithmetic: :func:`compute_uin_on_distribution`. Seed data:
``seeds/jurisdictions/LV/corporate_tax_rates.yaml``. No schema, no
posting — flagged in the build report as the corporate-tax compute
surface gap, mirrored from EE.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.jurisdictions.lv import (
    identifiers as _identifiers,  # noqa: F401  (registers lv_pvn/lv_regnum validators on first LV dispatch)
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
    PostingError,
    TaxTreatment,
    ValidationError,
)

_TWO_PLACES = money_quantum(2)

#: The PVN declaration return_type key in ``tax_return_box_definitions``
#: (seeds/jurisdictions/LV/tax_return_box_definitions.yaml).
PVN_RETURN_TYPE = "PVN"

# Company-side TaxCode.reporting_type tags that trigger the
# reverse-charge two-component fan-out (see the seed headers for the
# full LV tag convention).
RC_EU_ACQ_REPORTING_TYPES: frozenset[str] = frozenset({
    "rc_eu_acq_goods",
    "rc_eu_acq_services",
})
RC_THIRD_COUNTRY_REPORTING_TYPES: frozenset[str] = frozenset({
    "rc_third_country_services",
})
RC_DUAL_REPORTING_TYPES: frozenset[str] = (
    RC_EU_ACQ_REPORTING_TYPES | RC_THIRD_COUNTRY_REPORTING_TYPES
)

#: The three current PVN rates — each has a declaration row wired for
#: the reverse-charge legs (50/51/51.1 by rate; 54/63 sum tax at any of
#: these). A rate outside this set has no output row and is refused.
_RC_SUPPORTED_RATES: frozenset[Decimal] = frozenset(
    {Decimal("21"), Decimal("12"), Decimal("5")}
)


class ReverseChargeRateNotSupportedError(PostingError):
    """A reverse-charge acquisition line's rate isn't one of the rates
    the PVN declaration wires a reverse-charge leg for (21/12/5 — rows
    50/51/51.1) — posting it would silently mis-report the return."""


class DomesticReverseChargeNotSupportedError(PostingError):
    """Buyer-side domestic reverse charge (``rc_domestic_acq``,
    Arts. 141-143.4) is not postable this wave — the output-side
    declaration row was not primary-verified (only the row-62 deductible
    leg was), so the engine refuses rather than guessing a row."""


class LVCorporateTaxUnsupported(ValueError):
    """The UIN helper was asked for a regime/event combination this
    wave deliberately does not compute — refuse loudly rather than emit
    a wrong number."""


class LVTaxEngine:
    """Latvia PVN tax engine — implements the ``TaxEngine`` protocol
    (see ``services.tax_engine.__init__``)."""

    jurisdiction: str = "LV"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Single-treatment entry point — the FIRST element
        ``compute_components`` returns (for a reverse-charge line that
        is the output-role component, the shared engine convention)."""
        return self.compute_components(ctx)[0]

    def compute_components(self, ctx: PostingContext) -> list[TaxTreatment]:
        reporting_type = ctx.reporting_type or "no_tax"
        if reporting_type == "rc_domestic_acq":
            raise DomesticReverseChargeNotSupportedError(
                "buyer-side domestic reverse charge (rc_domestic_acq, PVN "
                "likums Art. 141-143.4) is not postable this wave: the "
                "deductible leg is verified to declaration row 62 but the "
                "output leg's row was not primary-verified — refusing "
                "rather than silently mis-reporting the PVN declaration. "
                "The SELLER side (rc_domestic_supply, row 41.1) is "
                "supported."
            )
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
            # Rate is percentage points (TaxCode.rate convention:
            # 21.000 == 21%) — divide by 100, the EE/NZ convention.
            return (base * rate / Decimal("100")).quantize(_TWO_PLACES)
        return Decimal("0")

    def _compute_standard(self, ctx: PostingContext) -> TaxTreatment:
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "PVN"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        return TaxTreatment(
            jurisdiction="LV",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=self._direction(ctx),
        )

    def _compute_reverse_charge(self, ctx: PostingContext) -> list[TaxTreatment]:
        """RC fan-out: one acquisition line emits BOTH an output-role
        component (self-assessed VAT — rows 50/51/51.1+55/56/56.1 for
        EU acquisitions, row 54 for third-country services) and an
        input-role component (deductible VAT — rows 64 / 63). Both
        share the same ``base``; ``extra["deductible_fraction"]``
        (default 1) scales ONLY the input component — the output
        liability is never partial (the EE §30 shape; Latvia's Art. 98
        proportion rides the same hook)."""
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        if rate not in _RC_SUPPORTED_RATES:
            supported = ", ".join(str(r) for r in sorted(_RC_SUPPORTED_RATES))
            raise ReverseChargeRateNotSupportedError(
                f"reverse-charge acquisition rate {rate} has no PVN "
                f"declaration row wired for it — supported rates are "
                f"{supported}% (rows 50/51/51.1). An unwired rate would "
                "land its base in no output row while rows 63/64 still "
                "deducted the input VAT — a silent asymmetry. Provision "
                "this line at a supported rate, or wire the new-rate row "
                "(same shape as rows 50/51/51.1)."
            )
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "RC_ACQ"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        deductible_fraction = Decimal("1")
        if ctx.extra and "deductible_fraction" in ctx.extra:
            deductible_fraction = Decimal(str(ctx.extra["deductible_fraction"]))
        input_tax = (tax * deductible_fraction).quantize(_TWO_PLACES)

        output = TaxTreatment(
            jurisdiction="LV",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction="output",
            notes=("reverse_charge_output",),
        )
        input_component = TaxTreatment(
            jurisdiction="LV",
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
        """Not used — PVN period summaries go through ``pvn_report``
        (data-driven ``generate_return``), the same convention as
        AU/EE/NZ/UK."""
        raise NotImplementedError(
            "LVTaxEngine.boxes is not used — call "
            "saebooks.jurisdictions.lv.tax.pvn_report(...) (which wraps "
            "tax_return_generator.generate_return(jurisdiction='LV', "
            "return_type='PVN', ...)) instead."
        )

    def validate(self, invoice: Any) -> list[ValidationError]:
        """LV pre-post checks. None yet — satisfies the protocol."""
        return []


async def pvn_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    manual_values: dict[str, Decimal] | None = None,
) -> Any:
    """Build a PVN declaration for the period — thin wrapper over the
    data-driven return calculator (the exact ``bas_report`` shape).

    ``manual_values`` injects the filer-entered rows (57 prior-period
    clawbacks, 65 agricultural compensation, 66 non-deductible part,
    67 corrections — all ``manual`` in the seed); absent values are an
    explicit 0 in the 60/S/P/70/80 formulas.

    Local import to avoid a module-load cycle: ``tax_return_generator``
    imports this package's account-type sets (via ``au.tax``) at import
    time — the same call-time-import rationale as ``bas_report``.
    """
    from saebooks.services.tax_return_generator import generate_return

    return await generate_return(
        session,
        company_id,
        jurisdiction="LV",
        return_type=PVN_RETURN_TYPE,
        from_date=from_date,
        to_date=to_date,
        manual_values=manual_values,
    )


# --------------------------------------------------------------------------
# UIN — uzņēmumu ienākuma nodoklis (distributed-profits corporate tax)
# --------------------------------------------------------------------------

_STANDARD_COEFF = Decimal("0.8")
_STANDARD_RATE = Decimal("0.20")
_ALT_COEFF = Decimal("0.85")
_ALT_CIT_RATE = Decimal("0.15")
_ALT_IIN_RATE = Decimal("0.06")

#: Regime keys — match corporate_tax_rates.yaml entity_scopes.
UIN_REGIME_STANDARD = "distributed_profit"
UIN_REGIME_ALT_INDIVIDUAL = "distributed_profit_alternative"

#: Events the helper computes. Deemed distributions stay under the
#: standard mechanics EVEN when the alternative regime is elected —
#: the 15% rate applies ONLY to dividends (VID bulletin 2025-12-23).
UIN_EVENT_DIVIDEND = "dividend"
UIN_EVENT_DEEMED = "deemed_distribution"


@dataclass(frozen=True, slots=True)
class UinDistributionResult:
    """The arithmetic of one distribution event.

    ``taxable_base`` is the grossed-up base (net ÷ coefficient);
    ``cit`` the company-level UIN; ``iin_withheld`` the 6% personal
    income tax withheld at distribution (alternative regime only,
    zero under the standard regime — single-layer taxation).
    """

    regime: str
    event: str
    net_distribution: Decimal
    taxable_base: Decimal
    cit: Decimal
    iin_withheld: Decimal

    @property
    def total_tax(self) -> Decimal:
        return self.cit + self.iin_withheld


def compute_uin_on_distribution(
    net_distribution: Decimal,
    *,
    regime: str = UIN_REGIME_STANDARD,
    event: str = UIN_EVENT_DIVIDEND,
) -> UinDistributionResult:
    """Latvia's distributed-profits UIN on one distribution event.

    Standard regime (UIN likums Art. 3(1) + 4(9), VERIFIED):
        base = net / 0.8;  CIT = base * 20%   (== net * 25%)
        No IIN withheld — single-layer taxation at the company.

    Alternative regime (2026-01-01+, Art. 3(5)/4.2, VERIFIED): applies
    to DIVIDENDS of companies owned exclusively by natural persons,
    elected all-or-nothing per tax period:
        base = net / 0.85;  CIT = base * 15%
        PLUS IIN = net * 6% withheld simultaneously.
    A deemed distribution under an active election still computes under
    the STANDARD mechanics (the 15% rate applies only to dividends) —
    this function encodes that rule rather than trusting the caller.

    Retained/reinvested profit is 0% by definition — there is no event
    to compute, so no "retained" regime key exists here.

    Raises :class:`LVCorporateTaxUnsupported` for anything else —
    never a silent wrong number.
    """
    if net_distribution < 0:
        raise LVCorporateTaxUnsupported(
            "negative net_distribution — a distribution reversal/return "
            "of capital is not a taxable event this helper computes."
        )
    if event not in (UIN_EVENT_DIVIDEND, UIN_EVENT_DEEMED):
        raise LVCorporateTaxUnsupported(
            f"unknown UIN event {event!r} — supported: "
            f"{UIN_EVENT_DIVIDEND!r}, {UIN_EVENT_DEEMED!r}."
        )
    if regime not in (UIN_REGIME_STANDARD, UIN_REGIME_ALT_INDIVIDUAL):
        raise LVCorporateTaxUnsupported(
            f"unknown UIN regime {regime!r} — supported: "
            f"{UIN_REGIME_STANDARD!r}, {UIN_REGIME_ALT_INDIVIDUAL!r}. "
            "(Retained profit is 0% — no event to compute.)"
        )

    effective_regime = regime
    if regime == UIN_REGIME_ALT_INDIVIDUAL and event == UIN_EVENT_DEEMED:
        # The 15% alternative rate applies ONLY to dividends; deemed
        # distributions stay 20%/÷0.8 even under an active election.
        effective_regime = UIN_REGIME_STANDARD

    if effective_regime == UIN_REGIME_STANDARD:
        base = (net_distribution / _STANDARD_COEFF).quantize(_TWO_PLACES)
        cit = (base * _STANDARD_RATE).quantize(_TWO_PLACES)
        iin = Decimal("0.00")
    else:
        base = (net_distribution / _ALT_COEFF).quantize(_TWO_PLACES)
        cit = (base * _ALT_CIT_RATE).quantize(_TWO_PLACES)
        iin = (net_distribution * _ALT_IIN_RATE).quantize(_TWO_PLACES)

    return UinDistributionResult(
        regime=regime,
        event=event,
        net_distribution=net_distribution,
        taxable_base=base,
        cit=cit,
        iin_withheld=iin,
    )


__all__ = [
    "PVN_RETURN_TYPE",
    "RC_DUAL_REPORTING_TYPES",
    "RC_EU_ACQ_REPORTING_TYPES",
    "RC_THIRD_COUNTRY_REPORTING_TYPES",
    "UIN_EVENT_DEEMED",
    "UIN_EVENT_DIVIDEND",
    "UIN_REGIME_ALT_INDIVIDUAL",
    "UIN_REGIME_STANDARD",
    "DomesticReverseChargeNotSupportedError",
    "LVCorporateTaxUnsupported",
    "LVTaxEngine",
    "ReverseChargeRateNotSupportedError",
    "UinDistributionResult",
    "compute_uin_on_distribution",
]
