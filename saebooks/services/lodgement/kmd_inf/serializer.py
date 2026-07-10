"""KMD-INF (VAT-return invoice annex) file serializer — e-MTA
manual-upload path.

Packet 2 of the kmd-inf-tsd scope (``~/.claude/plans/kmd-inf-tsd-scope.md``
§4/§7). Renders the row sets ``kmd_inf.generator.generate_kmd_inf`` already
computes (Packet 1) to both formats — XML primary, CSV secondary — mirroring
``services/lodgement/kmd/serializer.py``'s discipline exactly, adapted for a
**repeating-row** document instead of a flat box vector (see the module
docstring in ``mapping.py`` for the structural delta).

Input contract: this module serializes the generator's OWN typed row
dataclasses (``KmdInfPartARow`` / ``KmdInfPartBRow``, wrapped in
``KmdInfListing``) directly — no intermediate normalisation layer, unlike
``KmdFigures.from_*``, because the generator already emits the exact
per-row shape the file needs (Decimal amounts, an ``int`` row_no, a
``date``, optional strings). The one piece the generator does NOT carry is
the filer's e-MTA registry code (it works in ``company_id``, not
``regcode``) — that is supplied separately via ``KmdInfReportingContext``,
mirroring ``kmd.serializer.KmdReportingContext``.

⚠ Element/column names are PLACEHOLDER — see ``mapping.py``'s module
docstring for the full conformance-status note.

Rounding: euros (and the percentage rate column) to 2 dp, ``ROUND_HALF_UP``
— same convention and same UNVERIFIED tie-break as
``kmd.serializer._money``, reused here rather than re-derived.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lxml import etree

from saebooks.services.lodgement.kmd_inf.generator import (
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
)
from saebooks.services.lodgement.kmd_inf.mapping import (
    KMD_INF_ATTR_PERIOD_END,
    KMD_INF_ATTR_PERIOD_START,
    KMD_INF_ATTR_REGCODE,
    KMD_INF_CSV_DELIMITER,
    KMD_INF_CSV_ENCODING,
    KMD_INF_CSV_HEADER_PERIOD_END,
    KMD_INF_CSV_HEADER_PERIOD_START,
    KMD_INF_CSV_HEADER_REGCODE,
    KMD_INF_PART_A_COLUMNS,
    KMD_INF_PART_A_CONTAINER_ELEMENT,
    KMD_INF_PART_A_FIELD_NAMES,
    KMD_INF_PART_A_ROW_ELEMENT,
    KMD_INF_PART_B_COLUMNS,
    KMD_INF_PART_B_CONTAINER_ELEMENT,
    KMD_INF_PART_B_FIELD_NAMES,
    KMD_INF_PART_B_ROW_ELEMENT,
    KMD_INF_ROOT_ELEMENT,
    KMD_INF_SCHEMA_REF,
    KMD_INF_TAXONOMY_NS,
    KMD_INF_TAXONOMY_PREFIX,
)

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal) -> str:
    """Cent-precision string, 2 dp, ROUND_HALF_UP — same convention as
    ``kmd.serializer._money`` (deliberately reused, not re-derived).
    Also used for the ``rate`` column (a percentage, e.g. ``24.00``)."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _format_value(value: Any) -> str:
    """Type-driven cell/text formatter for a row field. Every
    ``KmdInfPartARow``/``KmdInfPartBRow`` field is one of these types —
    dispatch on type rather than field name so Part A and Part B share
    one formatter."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class KmdInfReportingContext:
    """The filer identity + period every KMD-INF file carries. Mirrors
    ``kmd.serializer.KmdReportingContext`` — kept as a separate type
    (not imported from the ``kmd`` package) so ``kmd_inf`` stays a
    self-contained sibling package, per the scope's package layout."""

    regcode: str
    period_start: date
    period_end: date


def _xml_row_element(
    tag: str, ns: str, row: KmdInfPartARow | KmdInfPartBRow, columns: tuple[str, ...], field_names: dict[str, str]
) -> etree._Element:
    row_el = etree.Element(etree.QName(ns, tag))
    for key in columns:
        sub = etree.SubElement(row_el, etree.QName(ns, field_names[key]))
        sub.text = _format_value(getattr(row, key))
    return row_el


def build_kmd_inf_xml_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Render a KMD-INF listing (Part A + Part B) as one XML document.

    Unlike ``kmd.build_kmd_xml_document`` (which emits all 28 boxes even
    when nil), a repeating-row listing has no "reported nil" concept — a
    period with zero qualifying rows emits an EMPTY ``OsaA``/``OsaB``
    container, not a placeholder row (scope §4: "N rows", N may be 0)."""
    nsmap = {KMD_INF_TAXONOMY_PREFIX: KMD_INF_TAXONOMY_NS}
    root = etree.Element(etree.QName(KMD_INF_TAXONOMY_NS, KMD_INF_ROOT_ELEMENT), nsmap=nsmap)
    root.set("schemaRef", KMD_INF_SCHEMA_REF)
    root.set(KMD_INF_ATTR_REGCODE, ctx.regcode)
    root.set(KMD_INF_ATTR_PERIOD_START, ctx.period_start.isoformat())
    root.set(KMD_INF_ATTR_PERIOD_END, ctx.period_end.isoformat())

    part_a_el = etree.SubElement(root, etree.QName(KMD_INF_TAXONOMY_NS, KMD_INF_PART_A_CONTAINER_ELEMENT))
    for row in listing.part_a:
        part_a_el.append(
            _xml_row_element(KMD_INF_PART_A_ROW_ELEMENT, KMD_INF_TAXONOMY_NS, row, KMD_INF_PART_A_COLUMNS, KMD_INF_PART_A_FIELD_NAMES)
        )

    part_b_el = etree.SubElement(root, etree.QName(KMD_INF_TAXONOMY_NS, KMD_INF_PART_B_CONTAINER_ELEMENT))
    for row in listing.part_b:
        part_b_el.append(
            _xml_row_element(KMD_INF_PART_B_ROW_ELEMENT, KMD_INF_TAXONOMY_NS, row, KMD_INF_PART_B_COLUMNS, KMD_INF_PART_B_FIELD_NAMES)
        )

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _build_csv(
    rows: list[KmdInfPartARow] | list[KmdInfPartBRow],
    ctx: KmdInfReportingContext,
    columns: tuple[str, ...],
    field_names: dict[str, str],
) -> bytes:
    """One header row + N data rows (a listing is genuinely multi-row,
    unlike KMD's single summary row — scope §4). Each data row repeats
    the header regcode/period as its leading three columns (PLACEHOLDER
    convention, see ``mapping.py``) so a row is self-describing even if
    extracted from the file in isolation."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=KMD_INF_CSV_DELIMITER, lineterminator="\r\n")
    header = [
        KMD_INF_CSV_HEADER_REGCODE,
        KMD_INF_CSV_HEADER_PERIOD_START,
        KMD_INF_CSV_HEADER_PERIOD_END,
        *(field_names[key] for key in columns),
    ]
    writer.writerow(header)
    for row in rows:
        writer.writerow([
            ctx.regcode,
            ctx.period_start.isoformat(),
            ctx.period_end.isoformat(),
            *(_format_value(getattr(row, key)) for key in columns),
        ])
    return buf.getvalue().encode(KMD_INF_CSV_ENCODING)


def build_kmd_inf_part_a_csv_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Part A (issued sales) CSV — separate file from Part B (scope §4:
    Part A and Part B have different column shapes, e.g. ``kmd_box_code``
    has no Part B equivalent and ``input_vat`` has no Part A equivalent,
    so one shared table would need optional/blank columns either way;
    e-MTA's own KMD-INF form itself splits A/B as two annex halves)."""
    return _build_csv(listing.part_a, ctx, KMD_INF_PART_A_COLUMNS, KMD_INF_PART_A_FIELD_NAMES)


def build_kmd_inf_part_b_csv_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Part B (received purchases) CSV — see ``build_kmd_inf_part_a_csv_document``."""
    return _build_csv(listing.part_b, ctx, KMD_INF_PART_B_COLUMNS, KMD_INF_PART_B_FIELD_NAMES)
