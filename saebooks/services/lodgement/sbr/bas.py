"""BAS SBR document generator — PUBLIC SHIM (generation stubbed).

The ``BasFigures`` input contract (normalised BAS GST labels) stays real — it is
pure data mapping the engine's tax figures onto the label set, and kept code
(``api/v1/tax_returns.py``) imports it. The XBRL *generation*
(``build_bas_document``) is part of the certified ATO transmission path and
raises ``NotImplementedError("commercial feature")`` in the open engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from saebooks.services.lodgement.sbr.xbrl import ReportingContext

_COMMERCIAL = (
    "commercial feature: ATO SBR BAS document generation is not available in "
    "the open engine"
)

#: Marks this module as the open-engine PUBLIC SHIM (certified ATO SBR XBRL
#: transmission stubbed out). The private build's real generator never defines
#: this, so tests can ``skipif(getattr(bas, "__OPEN_ENGINE_STUB__", False), ...)``
#: to auto-skip the certified-transmission assertion in the open tree while still
#: running it (unchanged) in the private build.
__OPEN_ENGINE_STUB__ = True


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
        """Build from a ``tax_returns.figures`` JSONB dict, tolerant of key style."""
        norm = {
            str(k).lower().replace("-", "").replace("_", ""): v
            for k, v in figures.items()
        }

        def pick(*keys: str) -> Decimal:
            for k in keys:
                if k in norm:
                    val = norm[k]
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
    raise NotImplementedError(_COMMERCIAL)
