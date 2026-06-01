"""BAS / IAS (Activity Statement) SBR document generator.

Maps the engine's computed BAS figures (``tax_engine.au.BASReport``, or the
``figures`` JSONB persisted on a ``tax_returns`` row) onto an XBRL instance
suitable for the lodge-server's ``/api/v1/bas/lodge`` route.

⚠ CONFORMANCE STATUS (A5 harvest, verified live 2026-06-02) — read first
------------------------------------------------------------------------
The ``_AS_CONCEPTS`` local-names, ``AS_TAXONOMY_NS`` and ``AS_SCHEMA_REF`` are
STILL PLACEHOLDERS, deliberately. A5 surfaced two blockers that make wiring the
"obvious" values WRONG:

  1. **AS.0004 (2025) migrated from XBRL to plain XML** (per the public AS BIG
     §4.2). This file builds an *XBRL* instance. The public SBR taxonomy server
     only hosts the older **AS.0001 v02.00 (2014)** XBRL artefact; the AS.0004
     XML namespace / schemaRef / element names are DSP-gated (SBR ShareFile, reg
     ticket uHkeDp0cepzOr8Uw) and may not be XBRL QNames at all. Whether the XML
     form even reuses the dotted-PascalCase concept names is UNCONFIRMED.
  2. The BAS-label→concept *bindings* for G1/G3/G10/G11 were **refuted** during
     A5: the underlying elements are real, but the "G3 - Other GST-free sales"
     style label evidence binding them to those fields was fabricated by the
     harvest model and does not exist in any authoritative SBR artefact.

Public AS.0001 reference values (authoritative, for the eventual swap):
  ns      http://sbr.gov.au/rprt/ato/as.0001.02.00.data
  schema  …/sbr_au_reports/ato/as/as_0001/as.0001.lodge.request.02.00.report.xsd
Per-label status (verified element QName / binding confidence):
  1A  GoodsAndServicesTax.Payable.Amount         (DE3139)  STRONG  — label linkbase corroborates
  1B  GoodsAndServicesTax.ClaimableCredits.Amount(DE752)   STRONG  — label linkbase corroborates
  G2  GoodsAndServicesTax.ExportSales.Amount     (DE652)   element real; binding natural (sibling of G3)
  9   Report.Statement.Summary.Net.Amount        (DE645)   candidate; confirm 9→DE645 in AS label linkbase
  G1  Income.SaleOfGoodsAndServices.Whole.Amount (DE661)   element real; G1 BINDING UNPROVEN
  G3  GoodsAndServicesTax.ExemptSales.Amount     (DE658)   element real; binding REFUTED
  G10 Expense.Capital.Amount    (DE650, generic bafpr dict) binding REFUTED (not a GST-form concept)
  G11 Expense.NonCapital.Amount (DE657, generic bafpr dict) binding REFUTED
Keystone to finish: the AS.0004 2025 MIG/MST/XSD (DSP hub) + EVTE validation.
See ~/.claude/plans/a5-artefact-harvest-result.json for full evidence/sources.
The golden-file test pins the current structure so the real-concept swap stays
a mechanical, reviewable diff.
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

# ⚠ PLACEHOLDERS — gated on the AS.0004 2025 XML MIG/XSD (see module docstring).
# Public AS.0001 reference (authoritative, NOT the AS.0004 XML target):
#   AS_TAXONOMY_NS = "http://sbr.gov.au/rprt/ato/as.0001.02.00.data"
#   AS_SCHEMA_REF  = ".../as_0001/as.0001.lodge.request.02.00.report.xsd"
AS_TAXONOMY_NS = "http://sbr.gov.au/PLACEHOLDER/ato/as"  # TODO(MIG): AS.0004 XML ns (gated)
AS_TAXONOMY_PREFIX = "as"
AS_SCHEMA_REF = "http://sbr.gov.au/PLACEHOLDER/ato/as.xsd"  # TODO(MIG): AS.0004 schemaRef (gated)

# ⚠ PLACEHOLDER concept local-names. Verified AS.0001-era candidates + their A5
# binding-confidence are in the module docstring; 1A/1B are STRONG, G3/G10/G11
# bindings were REFUTED. Do NOT promote any of these to live until the AS.0004
# MIG confirms the XML element names + bindings and EVTE validates.
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
    def from_bas_report(cls, report: Any) -> "BasFigures":
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
    def from_figures_json(cls, figures: dict[str, Any]) -> "BasFigures":
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
