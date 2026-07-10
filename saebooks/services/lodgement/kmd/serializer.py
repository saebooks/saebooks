"""EE KMD (VAT return) file serializer — e-MTA manual-upload path.

[EMTA-SUBMIT] confirms the manual filing channel: "Käibedeklaratsiooni ...
saab esitada e-teenuste keskkonnas e-MTA, sisestades andmeid käsitsi või
laadides XML- või CSV-formaadis failist." This module renders a computed
KMD box vector (28 boxes, see ``mapping.KMD_BOX_ORDER``) to both formats —
XML primary, CSV secondary (scope §5). X-Road transmission is out of scope
here; this produces the *file* a filer manually uploads.

⚠ Element/column names are PLACEHOLDER — see ``mapping.py``'s module
docstring for the full conformance-status note and the "one file to
correct" contract this and its golden-file test enforce.

Rounding: euros to the cent (2 dp) per scope §3.2 ("Eurodes sendi
täpsusega") — NOT whole-euro truncation like the AU SBR path
(``sbr/xbrl.py``'s ``_money`` truncates to whole dollars; deliberately not
reused here).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lxml import etree

from saebooks.services.lodgement.kmd.mapping import (
    KMD_ATTR_PERIOD_END,
    KMD_ATTR_PERIOD_START,
    KMD_ATTR_REGCODE,
    KMD_BOX_ORDER,
    KMD_CSV_DELIMITER,
    KMD_CSV_ENCODING,
    KMD_CSV_HEADER_PERIOD_END,
    KMD_CSV_HEADER_PERIOD_START,
    KMD_CSV_HEADER_REGCODE,
    KMD_FIELD_NAMES,
    KMD_ROOT_ELEMENT,
    KMD_SCHEMA_REF,
    KMD_TAXONOMY_NS,
    KMD_TAXONOMY_PREFIX,
)

_TWO_PLACES = Decimal("0.01")


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    """Cent-precision string ("sendi täpsusega", scope §3.2) — 2 dp,
    ROUND_HALF_UP (the scope's stated pragmatic default; direction is
    UNVERIFIED against the real XSD, see mapping.py)."""
    return str(_dec(value).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class KmdReportingContext:
    """The filer identity + period every KMD file carries."""

    regcode: str
    period_start: date
    period_end: date


@dataclass(frozen=True)
class KmdFigures:
    """Normalised KMD box vector — the generator's stable input contract.

    Keyed by the scope's box-code convention (``"1"``, ``"1-1"``,
    ``"3.1.1"``, ...). Missing boxes default to 0 (a KMD file always
    reports all 28 boxes explicitly, per [FORM]/[EMTA-FILL]'s cent-
    precision convention — a reported nil is not an absent field)."""

    boxes: dict[str, Decimal] = field(default_factory=dict)

    def amount(self, box_code: str) -> Decimal:
        return self.boxes.get(box_code, Decimal("0"))

    @classmethod
    def from_box_amounts(cls, amounts: dict[str, Any]) -> KmdFigures:
        """Build directly from a ``box_code -> Decimal``-ish mapping —
        e.g. ``tax_return_generator._evaluate_formula_boxes``'s return
        shape, or the ``_kmd_box_vector`` test helper's shape. Extra keys
        (the seed's internal ``*_DOMESTIC``/``*_RC`` helper boxes) are
        simply ignored — only ``mapping.KMD_BOX_ORDER`` codes are read."""
        return cls(boxes={code: _dec(amounts.get(code, 0)) for code in KMD_BOX_ORDER})

    @classmethod
    def from_tax_return_result(cls, result: Any) -> KmdFigures:
        """Build from a ``tax_return_generator.TaxReturnResult``."""
        return cls.from_box_amounts({code: b.amount for code, b in result.boxes.items()})

    @classmethod
    def from_figures_json(cls, figures: dict[str, Any]) -> KmdFigures:
        """Build from a ``tax_returns.figures`` JSONB dict (this module's
        persistence shape — see ``tax_return_generator.persist_return``).

        Matches box_code keys EXACTLY — unlike
        ``sbr.bas.BasFigures.from_figures_json``'s case/separator-
        stripping lookup, KMD box codes collide under that scheme:
        stripping ``-`` turns ``"1-1"`` into ``"11"``, which collides
        with the real box ``"11"`` (Täpsustused –). So: exact key match
        only, tolerating just the nested ``{"amount": x}`` unwrap
        ``persist_return`` may produce."""
        boxes: dict[str, Decimal] = {}
        for code in KMD_BOX_ORDER:
            val = figures.get(code)
            if isinstance(val, dict) and "amount" in val:
                val = val["amount"]
            boxes[code] = _dec(val) if val is not None else Decimal("0")
        return cls(boxes=boxes)


def build_kmd_xml_document(figures: KmdFigures, ctx: KmdReportingContext) -> bytes:
    """Render a KMD return as an XML document per the manual-upload path.

    Emits every one of the 28 official boxes explicitly, in form order,
    even when 0 — a reported nil is not an absent box (mirrors
    ``sbr.bas.build_bas_document``'s same nil-emission convention)."""
    nsmap = {KMD_TAXONOMY_PREFIX: KMD_TAXONOMY_NS}
    root = etree.Element(etree.QName(KMD_TAXONOMY_NS, KMD_ROOT_ELEMENT), nsmap=nsmap)
    root.set("schemaRef", KMD_SCHEMA_REF)
    root.set(KMD_ATTR_REGCODE, ctx.regcode)
    root.set(KMD_ATTR_PERIOD_START, ctx.period_start.isoformat())
    root.set(KMD_ATTR_PERIOD_END, ctx.period_end.isoformat())

    for box_code in KMD_BOX_ORDER:
        el = etree.SubElement(root, etree.QName(KMD_TAXONOMY_NS, KMD_FIELD_NAMES[box_code]))
        el.text = _money(figures.amount(box_code))

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_kmd_csv_document(figures: KmdFigures, ctx: KmdReportingContext) -> bytes:
    """Render a KMD return as a CSV document per the manual-upload path.

    One header row + one data row (a KMD return is a single period's box
    vector, not a per-line listing — unlike KMD-INF, out of scope here).
    Delimiter/encoding are PLACEHOLDER, see ``mapping.py``."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=KMD_CSV_DELIMITER, lineterminator="\r\n")
    header = [
        KMD_CSV_HEADER_REGCODE,
        KMD_CSV_HEADER_PERIOD_START,
        KMD_CSV_HEADER_PERIOD_END,
        *(KMD_FIELD_NAMES[code] for code in KMD_BOX_ORDER),
    ]
    row = [
        ctx.regcode,
        ctx.period_start.isoformat(),
        ctx.period_end.isoformat(),
        *(_money(figures.amount(code)) for code in KMD_BOX_ORDER),
    ]
    writer.writerow(header)
    writer.writerow(row)
    return buf.getvalue().encode(KMD_CSV_ENCODING)
