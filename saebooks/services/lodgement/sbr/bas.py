"""BAS / IAS (Activity Statement) SBR document generator.

Maps the engine's computed BAS figures (``tax_engine.au.BASReport``, or the
``figures`` JSONB persisted on a ``tax_returns`` row) onto an XBRL instance
suitable for the lodge-server's ``/api/v1/bas/lodge`` route.

⚠ CONFORMANCE STATUS — read before trusting the output
------------------------------------------------------
The label→figure mapping below (G1, G2, G3, G10, G11, 1A, 1B, 9) uses the
**public, stable BAS label codes**. The ``_AS_CONCEPTS`` element local-names,
the taxonomy namespace, and the schemaRef href are **PLACEHOLDERS** — the real
values come from the ATO SBR Activity Statement taxonomy + MIG (DSP-gated) and
MUST be dropped in and validated against the ATO EVTE before any real
lodgement. The XBRL *structure* is correct; the concept *names* are not yet.
The golden-file test pins the current structure so swapping the real concepts
in is a mechanical, reviewable diff.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from saebooks.services.lodgement.sbr.xbrl import (
    Fact,
    ReportingContext,
    build_instance,
)

# ⚠ PLACEHOLDERS pending the SBR Activity Statement taxonomy + MIG.
AS_TAXONOMY_NS = "http://sbr.gov.au/PLACEHOLDER/ato/as"  # TODO(MIG): real AS taxonomy ns
AS_TAXONOMY_PREFIX = "as"
AS_SCHEMA_REF = "http://sbr.gov.au/PLACEHOLDER/ato/as.xsd"  # TODO(MIG): real schemaRef

# ⚠ PLACEHOLDER concept local-names — modelled on the public BAS label codes.
# Replace each value with the authoritative AS taxonomy concept from the MIG.
_AS_CONCEPTS: dict[str, str] = {
    "G1": "TotalSalesIncludingGST",
    "G2": "ExportSales",
    "G3": "OtherGSTFreeSales",
    "G10": "CapitalPurchasesIncludingGST",
    "G11": "NonCapitalPurchasesIncludingGST",
    "1A": "GSTOnSales",
    "1B": "GSTOnPurchases",
    "9": "NetGSTAmount",
}


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class BasFigures:
    """Normalised BAS GST labels — the generator's stable input contract."""

    g1: Decimal = Decimal("0")
    g2: Decimal = Decimal("0")
    g3: Decimal = Decimal("0")
    g10: Decimal = Decimal("0")
    g11: Decimal = Decimal("0")
    label_1a: Decimal = Decimal("0")
    label_1b: Decimal = Decimal("0")

    @property
    def net_9(self) -> Decimal:
        """Label 9 — net amount owing (1A) less credits (1B)."""
        return self.label_1a - self.label_1b

    @classmethod
    def from_bas_report(cls, report: Any) -> BasFigures:
        """Build from a ``tax_engine.au.BASReport`` (each field is a ``BASLine``)."""
        return cls(
            g1=_dec(report.g1.amount),
            g2=_dec(report.g2.amount),
            g3=_dec(report.g3.amount),
            g10=_dec(report.g10.amount),
            g11=_dec(report.g11.amount),
            label_1a=_dec(report.label_1a.amount),
            label_1b=_dec(report.label_1b.amount),
        )

    @classmethod
    def from_figures_json(cls, figures: dict[str, Any]) -> BasFigures:
        """Build from a ``tax_returns.figures`` JSONB dict, tolerant of key style.

        Accepts label keys in any of: ``G1``/``g1``, ``1A``/``label_1a``/``1a``.
        Unknown keys are ignored; missing labels default to 0.
        """
        norm = {str(k).lower().replace("-", "").replace("_", ""): v for k, v in figures.items()}

        def pick(*keys: str) -> Decimal:
            for k in keys:
                if k in norm:
                    val = norm[k]
                    # tolerate nested {"amount": x} (BASLine-style) JSON
                    if isinstance(val, dict) and "amount" in val:
                        val = val["amount"]
                    return _dec(val)
            return Decimal("0")

        return cls(
            g1=pick("g1"),
            g2=pick("g2"),
            g3=pick("g3"),
            g10=pick("g10"),
            g11=pick("g11"),
            label_1a=pick("1a", "label1a", "gstcollected"),
            label_1b=pick("1b", "label1b", "gstpaid"),
        )


def build_bas_document(figures: BasFigures, ctx: ReportingContext) -> bytes:
    """Render a BAS/IAS Activity Statement as XBRL instance bytes.

    Emits a fact per non-derived label plus the derived net (label 9). Zero
    labels are still emitted so the document is explicit about reported nils
    (the ATO distinguishes a reported 0 from an absent label).
    """
    pairs = [
        ("G1", figures.g1),
        ("G2", figures.g2),
        ("G3", figures.g3),
        ("G10", figures.g10),
        ("G11", figures.g11),
        ("1A", figures.label_1a),
        ("1B", figures.label_1b),
        ("9", figures.net_9),
    ]
    facts = [
        Fact(AS_TAXONOMY_NS, _AS_CONCEPTS[label], amount) for label, amount in pairs
    ]
    return build_instance(
        facts,
        ctx,
        taxonomy_ns=AS_TAXONOMY_NS,
        taxonomy_prefix=AS_TAXONOMY_PREFIX,
        schema_ref=AS_SCHEMA_REF,
    )
