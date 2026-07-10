"""EE tax engine — käibemaks (VAT), including the reverse-charge
two-component fan-out.

KMD-formula support Packet 3 (see
~/.claude/plans/kmd-formula-support-scope.md §3.4). Prerequisite (point
1, plumbed in ``services.journal._apply_tax_treatment``): the
per-jurisdiction posting dispatcher now resolves the company's actual
``TaxEngine`` via ``Company.jurisdiction`` instead of hardcoding AU — EE
is the first non-AU engine this dispatcher actually reaches (NZ/UK
remain stubs). Ordinary EE VAT lines behave identically to AU's
algorithm: direction from account_type (``_INPUT_TYPES``/
``_OUTPUT_TYPES``, imported from ``tax_engine.au`` rather than
duplicated — the mapping is jurisdiction-neutral, GL account semantics,
not a VAT rule), base = the line amount, tax = ``gst_amount`` when the
caller supplies it, else ``base * rate / 100``.

Reverse charge (points 2/3): an EU-acquisition purchase line — company-
side ``TaxCode.reporting_type`` in ``RC_DUAL_REPORTING_TYPES``
("rc_eu_acq_goods" / "rc_eu_acq_services", the SAME tags
``tax_return_box_definitions.yaml``'s KMD box 6/6.1 already read) — must
emit BOTH an output-role component (the self-assessed VAT payable —
feeds KMD box 1's base, and therefore box 4's rate-formula) and an
input-role component (the deductible input VAT — feeds KMD box 5) from
the SAME journal line. ``journal_line_tax_components`` is already
1:many-ready (``component_role`` / ``direction`` / ``sequence``, M1.5 ·
T2) — this needs no schema change, only a ``compute_components``
override that returns two ``TaxTreatment`` rows instead of one; the
dispatcher (``services.journal._apply_tax_treatment``) turns each
returned treatment into its own component row.

Scope narrowing (flagged, see this packet's build report): this packet
implements FULL deduction only — both components carry the same ``tax``
(and ``base``) amount, so a balanced RC posting nets to zero effect on
box 12/13 (scope §6's "output 960 = input 960" canonical proof). A
partial-deduction variant (§30, max 50%) is explicitly out of this
packet's required test list; ``PostingContext.extra["deductible_fraction"]``
is read (default ``1``) so that follow-up needs no further engine
restructuring — only a caller that starts passing a fraction < 1.

Void/reversal caveat (documented, not fixed by this packet — mirrors an
existing, already-accepted limitation): ``services.journal.reverse()``
posts reversal lines with ``tax_code_id`` copied but ``gst_amount=None``
so they emit NO tax component (T2's own regression test pins this for
the single-component AU/EE case). The RC-FANOUT boxes this packet adds
(the "*_RC" internal boxes in the EE KMD seed) inherit the same
property: voiding a posted reverse-charge line does not itself emit an
offsetting component, so — exactly like every existing
``sum_tax_amount_for_codes`` box today — a void relies on the entry
being excluded from the aggregation window (archived/date-filtered), not
on a symmetric negative component. Not a new gap this packet introduces.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from saebooks.services.tax_engine.au import _INPUT_TYPES, _OUTPUT_TYPES
from saebooks.services.tax_engine.types import (
    PostingContext,
    PostingError,
    TaxTreatment,
    ValidationError,
)

# Company-side TaxCode.reporting_type tags that trigger the reverse-charge
# two-component fan-out — the SAME tags tax_return_box_definitions.yaml's
# KMD box 6/6.1 already read (see that seed's header + tax_codes.yaml's
# header for the full convention list). Only EU-acquisition purchase-side
# codes fan out to an output+input pair here: RC_DOMESTIC (seller-side
# KMS §41^1, KMD box 9) is a plain single-component sale-side tag, not
# fanned out — the counterparty who self-assesses under KMS §41^1 is the
# BUYER, on their own ledger, tagged with one of these codes; this
# company's own §41^1 SALE never carries VAT to self-assess.
RC_DUAL_REPORTING_TYPES: frozenset[str] = frozenset({
    "rc_eu_acq_goods",
    "rc_eu_acq_services",
})

# Finding 1 (rate-aware RC routing): the KMD seed now wires per-rate
# reverse-charge legs into the three current positive-rate output boxes
# the [FORM] "Lahtrites 1, 2 ja 2^2" juhis names — box 1 (24%), box 2
# (9%) and box 2-2 (13%) — via rate-pinned "@24"/"@9"/"@13" role boxes
# (1_RC / 2_RC / 2-2_RC), and box 4's rate-formula taxes each base at its
# own rate. So an rc_eu_acq_* line at any of these three rates now routes
# correctly and is accepted. A rate OUTSIDE this set (e.g. the 20%/22%
# legacy standard rates, or 5%) has no output box wired for it — posting
# it would land the base in NO output box while box 5 still deducted the
# input VAT, a silent asymmetry — so those genuinely-unsupported vintages
# are still rejected loudly (see scope §3.4: "the slice boundary must be
# stated to the user, not buried"). Adding a legacy-rate leg is the same
# mechanical change as the three above if a historical-period RC ever
# needs it.
_RC_SUPPORTED_RATES: frozenset[Decimal] = frozenset(
    {Decimal("24"), Decimal("9"), Decimal("13")}
)


class ReverseChargeRateNotSupportedError(PostingError):
    """A reverse-charge EU-acquisition line's rate isn't one of the rates
    the KMD boxes wire a reverse-charge leg for (24% / 9% / 13%, the
    current positive-rate output boxes 1 / 2 / 2-2) — posting it would
    silently mis-report the KMD return (see the ``_RC_SUPPORTED_RATES``
    comment above)."""


_TWO_PLACES = Decimal("0.01")


class EETaxEngine:
    """Estonia käibemaks (VAT) tax engine — implements the ``TaxEngine``
    protocol (see ``services.tax_engine.__init__``)."""

    jurisdiction: str = "EE"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Single-treatment entry point — the FIRST element
        ``compute_components`` returns (matches every other engine's
        single-treatment shape for a non-reverse-charge line; for a
        reverse-charge line this is the output-role component, the same
        convention ``services.journal._apply_tax_treatment`` uses for
        the ``journal_lines.tax_treatment`` JSONB snapshot)."""
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

    def _derive_tax(self, ctx: PostingContext, *, rate: Decimal, base: Decimal) -> Decimal:
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
            jurisdiction="EE",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=self._direction(ctx),
        )

    def _compute_reverse_charge(self, ctx: PostingContext) -> list[TaxTreatment]:
        """RC-FANOUT (scope §3.4): one EU-acquisition purchase line
        emits BOTH an output-role component (self-assessed VAT payable
        — KMD box 1 base / box 4 via the rate-formula) and an
        input-role component (deductible input VAT — KMD box 5).

        Both components share the same ``base`` (the acquisition's net
        value — a reverse charge does not change the taxable base, only
        who self-assesses the VAT) and, for full deduction, the same
        ``tax``. ``ctx.extra["deductible_fraction"]`` (default ``1``)
        scales ONLY the input-role component — the output-role
        liability is never partial; only the buyer's input-credit
        entitlement can be capped (KMS §30).
        """
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        if rate not in _RC_SUPPORTED_RATES:
            supported = ", ".join(str(r) for r in sorted(_RC_SUPPORTED_RATES))
            raise ReverseChargeRateNotSupportedError(
                f"reverse-charge EU-acquisition rate {rate} has no KMD "
                f"output box wired for it — the supported rates are "
                f"{supported}% (boxes 1 / 2 / 2-2, the current "
                "positive-rate output boxes). A 20%/22% legacy-rate or 5% "
                "reverse charge would land its base in no output box while "
                "box 5 still deducted the input VAT — a silent asymmetry. "
                "Provision this line at a supported rate, or add the "
                "legacy-rate leg (same shape as 1_RC/2_RC/2-2_RC)."
            )
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "RC_EU_ACQ"
        base = ctx.amount
        tax = self._derive_tax(ctx, rate=rate, base=base)

        deductible_fraction = Decimal("1")
        if ctx.extra and "deductible_fraction" in ctx.extra:
            deductible_fraction = Decimal(str(ctx.extra["deductible_fraction"]))
        input_tax = (tax * deductible_fraction).quantize(_TWO_PLACES)

        output = TaxTreatment(
            jurisdiction="EE",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction="output",
            notes=("reverse_charge_output",),
        )
        input_component = TaxTreatment(
            jurisdiction="EE",
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
        """Not used — KMD period summaries go through
        ``tax_return_generator.generate_return(jurisdiction="EE",
        return_type="KMD")``, not this protocol method (mirrors AU's own
        ``au.py`` note that ``bas_report`` — not ``AUTaxEngine.boxes`` —
        is the production reporting path)."""
        raise NotImplementedError(
            "EETaxEngine.boxes is not used — call "
            "saebooks.services.tax_return_generator.generate_return("
            "jurisdiction='EE', return_type='KMD', ...) instead."
        )

    def validate(self, invoice: Any) -> list[ValidationError]:
        """EE pre-post checks. Stub at Packet 3 — no EE-specific
        pre-post validation exists yet; satisfies the protocol."""
        return []
