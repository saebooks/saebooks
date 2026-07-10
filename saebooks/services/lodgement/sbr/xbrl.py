"""SBR XBRL primitives — PUBLIC SHIM (ATO document generation stubbed).

The private build generates ATO SBR XBRL instances against the DSP-gated SBR
MIG taxonomies. That generation is part of the certified-transmission path and
is NOT shipped in the open repo. (Estonian KMD file generation *is* open — see
``services/lodgement/kmd/``.)

The data shapes (``ReportingContext``, ``Fact``) stay real so callers can build
inputs; the document builders (``build_instance``, ``envelope_parts``, the
``XbrlInstance`` builder) raise ``NotImplementedError("commercial feature")``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_COMMERCIAL = (
    "commercial feature: ATO SBR XBRL document generation is not available in "
    "the open engine"
)


@dataclass(frozen=True)
class ReportingContext:
    """XBRL reporting context — entity ABN + reporting period."""

    abn: str
    period_start: date
    period_end: date
    context_id: str = "ctx-period"
    unit_id: str = "AUD"


@dataclass(frozen=True)
class Fact:
    """A single monetary XBRL fact."""

    concept_ns: str
    concept_name: str
    value: Decimal
    decimals: str = "0"


def build_instance(
    facts: list[Fact],
    ctx: ReportingContext,
    *,
    taxonomy_ns: str,
    taxonomy_prefix: str,
    schema_ref: str,
) -> bytes:
    raise NotImplementedError(_COMMERCIAL)


class XbrlInstance:
    """Public symbol preserved; ATO XBRL generation is a commercial feature."""

    def __init__(
        self,
        *,
        taxonomy_ns: str,
        taxonomy_prefix: str,
        schema_ref: str,
        unit_id: str = "AUD",
    ) -> None:
        raise NotImplementedError(_COMMERCIAL)


def envelope_parts(document: bytes) -> tuple[str, str]:
    raise NotImplementedError(_COMMERCIAL)
