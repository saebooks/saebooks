"""BAS / IAS (Activity Statement) SBR document generator — COMMUNITY EDITION STUB.

The community (AGPL) edition ships the BAS *figure* mapping
(``BasFigures`` — normalises the engine's computed labels onto the public
BAS label codes G1/G2/G3/G10/G11/1A/1B/9) but not the regulator-facing XBRL
*document* generator. Building + validating the actual SBR Activity
Statement business document (concept QNames, taxonomy namespace, schemaRef
per the ATO SBR MIG, EVTE conformance) is a commercial SAE Books feature —
see CHARTER.md / LICENSING.md. ``build_bas_document`` raises
``NotImplementedError`` in this edition.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from saebooks.services.lodgement.sbr.xbrl import ReportingContext


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

    COMMUNITY EDITION STUB — always raises. Building + validating the
    regulator-conformant SBR Activity Statement business document (the
    real ATO taxonomy concepts, schemaRef, and EVTE-tested structure) is
    a commercial SAE Books feature; the community edition ships the box
    definitions and ``BasFigures`` mapping above but not this generator.
    """
    raise NotImplementedError(
        "Certified e-lodgement is a commercial SAE Books feature; the community "
        "edition ships box definitions + the return calculator but not the "
        "regulator transmission adapters. See CHARTER.md / LICENSING.md."
    )
