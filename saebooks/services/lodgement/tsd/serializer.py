"""TSD (income + social + withholding tax return) file serializer — e-MTA
manual-upload path, MAIN + Lisa 1.

Packet 5 of the kmd-inf-tsd scope (``~/.claude/plans/kmd-inf-tsd-scope.md``
§4/§7). Renders the MAIN totals + Lisa-1 row set ``tsd.generator.generate_tsd``
already computes (Packet 4) to both formats — XML primary, CSV secondary —
mirroring ``services/lodgement/kmd_inf/serializer.py``'s discipline, adapted
for TSD's own structural shape: one aggregate MAIN block (like KMD's flat
vector) *plus* one repeating Lisa-1 listing (like a KMD-INF part), in a
single XML document (see the module docstring in ``mapping.py`` for the
full structural-delta note).

Input contract: this module serializes the generator's own typed
``TsdListing`` (``TsdMainTotals`` + ``list[TsdLisa1Row]``) directly — no
intermediate normalisation layer, same posture as
``kmd_inf.serializer``. The filer's e-MTA registry code is supplied
separately via ``TsdReportingContext`` (``TsdListing`` works in
``company_id``, not ``regcode`` — mirrors both siblings' reporting-context
split).

⚠ Element/column names are PLACEHOLDER — see ``mapping.py``'s module
docstring for the full conformance-status note.

Rounding: euros to 2 dp, ``ROUND_HALF_UP`` — same convention and same
UNVERIFIED tie-break as ``kmd.serializer._money`` / ``kmd_inf.serializer._money``,
reused here rather than re-derived (self-contained sibling package, not
imported from either).

Persistence (scope §3.3): TSD is a repeating-row annex, so — unlike
``tax_return_generator.persist_return``'s flat ``box_code -> {amount,...}``
JSONB shape, which cannot hold a row list — ``persist_tsd_return`` below
persists a **list-shaped** ``figures`` payload (``{"main": {...},
"lisa1": [...], "errors": [...]}``) into the same ``tax_returns`` table
(``return_type="TSD"``), the "dedicated persistence path" the scope left
as an open decision for whichever packet ships the serializer. Includes
the full row (``employee_id``/``pay_run_id`` too) for audit provenance
even though those two keys are deliberately excluded from the *file*
export (see ``mapping.py``'s module docstring) — the persisted record and
the filed document are different consumers with different needs.

⚠ **isikukood is MASKED in the persisted ``figures`` (critic round 2
fix)**, not written plaintext — see ``_mask_isikukood``/
``_asdict_jsonable`` below. The generic ``GET /api/v1/tax_returns`` /
``GET /api/v1/tax_returns/{id}`` routes (``api/v1/tax_returns.py``)
return ``figures`` verbatim to any bearer-authenticated caller in the
tenant with no PII-specific gate — the same surface
``employees.py``'s dedicated ``GET /employees/{id}/tfn`` route protects
with a permission check. Rather than bolt a TSD-specific gate onto that
generic route (out of this packet's scope), the fix removes the
plaintext at the source: ``employee_id`` already gives the persisted
row full audit-provenance traceability back to
``Employee.isikukood_encrypted``, so the JSONB copy never needs the
live value. The XML/CSV file export is unaffected — it still carries
the real plaintext isikukood, its one legitimate destination (e-MTA
filing).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lxml import etree
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.tax_return import TaxReturn, TaxReturnStatus
from saebooks.services.lodgement.tsd.generator import (
    TsdDataQualityError,
    TsdLisa1Row,
    TsdLisa2ARow,
    TsdLisa2Listing,
    TsdLisa3Header,
    TsdLisa4Header,
    TsdLisa5Header,
    TsdLisa6Listing,
    TsdLisa7Listing,
    TsdListing,
    TsdMainTotals,
)
from saebooks.services.lodgement.tsd.mapping import (
    TSD_CSV_BOM,
    TSD_CSV_DELIMITER,
    TSD_CSV_ENCODING,
    TSD_EL_A2_ISIK,
    TSD_EL_A2_ISIK_LIST,
    TSD_EL_A2_MVT,
    TSD_EL_A2_VM,
    TSD_EL_A_ISIK,
    TSD_EL_A_ISIK_LIST,
    TSD_EL_B2_ISIK,
    TSD_EL_B2_ISIK_LIST,
    TSD_EL_B2_MVT,
    TSD_EL_B2_REASON_EXPLANATION,
    TSD_EL_B2_VM,
    TSD_EL_FORM,
    TSD_EL_INVFOND,
    TSD_EL_INVFOND_LIST,
    TSD_EL_INVFOND_VM,
    TSD_EL_ISIK_KOOD,
    TSD_EL_ISIK_KOOD_2A,
    TSD_EL_ISIK_KOOD_2B,
    TSD_EL_ISIK_NIMI_2A,
    TSD_EL_ISIK_NIMI_2B,
    TSD_EL_L6_1,
    TSD_EL_L6_1_LIST,
    TSD_EL_L6_2,
    TSD_EL_L6_2_LIST,
    TSD_EL_L6_3,
    TSD_EL_L6_3_LIST,
    TSD_EL_L7_1B,
    TSD_EL_L7_1B_LIST,
    TSD_EL_L7_1C,
    TSD_EL_L7_1C_LIST,
    TSD_EL_L7_2,
    TSD_EL_L7_2_LIST,
    TSD_EL_L7_2B,
    TSD_EL_L7_2B_LIST,
    TSD_EL_L7_4,
    TSD_EL_L7_4_LIST,
    TSD_EL_LISA1,
    TSD_EL_LISA2,
    TSD_EL_LISA3,
    TSD_EL_LISA4,
    TSD_EL_LISA5,
    TSD_EL_LISA6,
    TSD_EL_LISA7,
    TSD_EL_LOAD_METHOD,
    TSD_EL_MONTH,
    TSD_EL_MVT_LIST,
    TSD_EL_REGKOOD,
    TSD_EL_VM,
    TSD_EL_VM_LIST,
    TSD_EL_YEAR,
    TSD_FORM_TSD,
    TSD_LISA1_CSV_COLUMNS,
    TSD_LISA2_A_CSV_COLUMNS,
    TSD_LISA2_A_CSV_TEXT_CODES,
    TSD_LISA2_A_MVT_ELEMENTS,
    TSD_LISA2_A_VM_ELEMENTS,
    TSD_LISA2_B_MVT_ELEMENTS,
    TSD_LISA2_B_VM_ELEMENTS,
    TSD_LISA2_INVFOND_ELEMENTS,
    TSD_LISA2_INVFOND_VM_ELEMENTS,
    TSD_LISA2_TOTALS_ELEMENTS,
    TSD_LISA3_ELEMENTS,
    TSD_LISA4_ELEMENTS,
    TSD_LISA5_ELEMENTS,
    TSD_LISA6_HEADER_ELEMENTS,
    TSD_LISA6_INT_FIELDS,
    TSD_LISA6_ROW1_ELEMENTS,
    TSD_LISA6_ROW2_ELEMENTS,
    TSD_LISA6_ROW3_ELEMENTS,
    TSD_LISA7_HEADER_ELEMENTS,
    TSD_LISA7_INT_FIELDS,
    TSD_LISA7_ROW1B_ELEMENTS,
    TSD_LISA7_ROW1C_ELEMENTS,
    TSD_LISA7_ROW2_ELEMENTS,
    TSD_LISA7_ROW2B_ELEMENTS,
    TSD_LISA7_ROW4_ELEMENTS,
    TSD_LOAD_METHOD_NEW,
    TSD_MAIN_CSV_COLUMNS,
    TSD_MAIN_ELEMENTS,
    TSD_ROOT_ELEMENT,
    TSD_VM_ELEMENTS,
    payment_type_code,
)

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal) -> str:
    """Cent-precision string, 2 dp, ROUND_HALF_UP — same convention as
    ``kmd.serializer._money`` / ``kmd_inf.serializer._money`` (deliberately
    reused, not re-derived)."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _xml_text(value: Any) -> str:
    """Element-text formatter — Decimal to 2 dp, date ISO, else str."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _main_value(main: TsdMainTotals, key: str) -> Decimal:
    """Resolve a MAIN roll-up value, including the synthesised combined
    unemployment premium (c116_Tk = employee + employer)."""
    if key == "_unemployment_total":
        return main.total_unemployment_employee + main.total_unemployment_employer
    return getattr(main, key)


def _vm_value(row: TsdLisa1Row, key: str) -> Any:
    """Resolve a Lisa-1 payment (Vm) value; maps the payment-type token to its
    official ``c1020_ValiKood`` code."""
    if key == "payment_type_code":
        return payment_type_code(row.payment_type_code)
    return getattr(row, key)


def _group_by_person(rows: list[TsdLisa1Row]) -> list[tuple[str, list[TsdLisa1Row]]]:
    """Group Lisa-1 rows by isikukood, preserving first-appearance order — one
    ``tsd_L1_A_Isik`` per person, its payments as ``tsd_L1_A_Vm`` children."""
    order: list[str] = []
    by_person: dict[str, list[TsdLisa1Row]] = {}
    for row in rows:
        if row.isikukood not in by_person:
            by_person[row.isikukood] = []
            order.append(row.isikukood)
        by_person[row.isikukood].append(row)
    return [(kood, by_person[kood]) for kood in order]


# =============================================================================
# Module 1 (ee-frontier-build-plan.md §"MODULE 1") — Lisa 2-7 block builders.
#
# Shared discipline across all six annexes below:
#  * A field is OMITTED (element not written) when its value is ``None`` —
#    this mirrors the XSD's own ``minOccurs="0"`` for every field modelled
#    here, and matters for correctness, not just tidiness: emitting an empty
#    string for a ``None`` on an ``xs:decimal``/``xs:long`` field FAILS
#    validation (empty content is not a valid number), whereas omitting the
#    element entirely is exactly what "optional" means. Lisa 1's
#    ``TSD_VM_ELEMENTS`` loop never hit this because every ``TsdLisa1Row``
#    field is mandatory (never ``None``) — Lisa 2-7's rows/headers are
#    mostly OPTIONAL fields, so ``_append_optional_fields`` below is the
#    general-purpose version of that loop.
#  * ``int_fields`` routes a value through plain ``str(int(...))`` instead
#    of the 2dp money formatter — REQUIRED for every ``xs:long`` field
#    (year/month/etc.); passing a Decimal through ``_money`` would emit
#    "2023.00" for an ``xs:long``, which fails XSD validation (verified by
#    direct schema lookup per field — see mapping.py's per-annex comments).
# =============================================================================


def _append_optional_fields(
    parent: etree._Element,
    obj: Any,
    elements: tuple[tuple[str, str], ...],
    *,
    int_fields: frozenset[str] = frozenset(),
) -> None:
    """Append one child element per (attr, element_name) pair in ``elements``
    whose resolved value is not ``None``; skip (omit) ``None`` values."""
    for attr, element_name in elements:
        value = getattr(obj, attr)
        if value is None:
            continue
        if attr in int_fields:
            etree.SubElement(parent, element_name).text = str(int(value))
        else:
            etree.SubElement(parent, element_name).text = _xml_text(value)


def _group_by(rows: list[Any], key_attr: str) -> list[tuple[Any, list[Any]]]:
    """Generic version of ``_group_by_person`` — group ``rows`` by
    ``getattr(row, key_attr)``, preserving first-appearance order."""
    order: list[Any] = []
    by_key: dict[Any, list[Any]] = {}
    for row in rows:
        key = getattr(row, key_attr)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(row)
    return [(key, by_key[key]) for key in order]


def _append_lisa2_mvt(parent: etree._Element, mvt_rows: tuple, mvt_list_el: str, mvt_el: str, elements: tuple) -> None:
    if not mvt_rows:
        return
    mvt_list = etree.SubElement(parent, mvt_list_el)
    for mvt in mvt_rows:
        mvt_node = etree.SubElement(mvt_list, mvt_el)
        _append_optional_fields(mvt_node, mvt, elements)


def _append_lisa2_block(root: etree._Element, listing: TsdLisa2Listing) -> None:
    """Append ``tsd_L2_0`` (non-resident payments/withholding) —
    ``aIsikList``/``bIsikList``/``invFondList`` + annex totals, XSD order.
    ``TsdLisa2Listing.a_rows``/``b_rows``/``inv_fond_rows`` are FLAT (one
    row per payment, same discipline as ``TsdLisa1Row``) — grouped here by
    isikukood/fund_code, mirroring ``_group_by_person`` above."""
    lisa2 = etree.SubElement(root, TSD_EL_LISA2)

    if listing.a_rows:
        a_list = etree.SubElement(lisa2, TSD_EL_A2_ISIK_LIST)
        for isikukood, payments in _group_by(listing.a_rows, "isikukood"):
            isik = etree.SubElement(a_list, TSD_EL_A2_ISIK)
            etree.SubElement(isik, TSD_EL_ISIK_KOOD_2A).text = isikukood
            if payments[0].name is not None:
                etree.SubElement(isik, TSD_EL_ISIK_NIMI_2A).text = payments[0].name
            vm_list = etree.SubElement(isik, TSD_EL_VM_LIST)
            for row in payments:
                vm = etree.SubElement(vm_list, TSD_EL_A2_VM)
                _append_optional_fields(vm, row, TSD_LISA2_A_VM_ELEMENTS)
                _append_lisa2_mvt(vm, row.mvt, TSD_EL_MVT_LIST, TSD_EL_A2_MVT, TSD_LISA2_A_MVT_ELEMENTS)

    if listing.b_rows:
        b_list = etree.SubElement(lisa2, TSD_EL_B2_ISIK_LIST)
        for isikukood, payments in _group_by(listing.b_rows, "isikukood"):
            isik = etree.SubElement(b_list, TSD_EL_B2_ISIK)
            etree.SubElement(isik, TSD_EL_ISIK_KOOD_2B).text = isikukood
            if payments[0].name is not None:
                etree.SubElement(isik, TSD_EL_ISIK_NIMI_2B).text = payments[0].name
            vm_list = etree.SubElement(isik, TSD_EL_VM_LIST)
            for row in payments:
                vm = etree.SubElement(vm_list, TSD_EL_B2_VM)
                _append_optional_fields(vm, row, TSD_LISA2_B_VM_ELEMENTS)
                _append_lisa2_mvt(vm, row.mvt, TSD_EL_MVT_LIST, TSD_EL_B2_MVT, TSD_LISA2_B_MVT_ELEMENTS)
                if row.reason_explanation is not None:
                    etree.SubElement(vm, TSD_EL_B2_REASON_EXPLANATION).text = row.reason_explanation

    if listing.totals is not None:
        _append_optional_fields(lisa2, listing.totals, TSD_LISA2_TOTALS_ELEMENTS)

    if listing.inv_fond_rows:
        fond_list = etree.SubElement(lisa2, TSD_EL_INVFOND_LIST)
        for _fund_code, payments in _group_by(listing.inv_fond_rows, "fund_code"):
            fond = etree.SubElement(fond_list, TSD_EL_INVFOND)
            head = payments[0]
            _append_optional_fields(fond, head, TSD_LISA2_INVFOND_ELEMENTS)
            vm_list = etree.SubElement(fond, TSD_EL_VM_LIST)
            for row in payments:
                vm = etree.SubElement(vm_list, TSD_EL_INVFOND_VM)
                _append_optional_fields(vm, row, TSD_LISA2_INVFOND_VM_ELEMENTS)


def _append_lisa3_block(root: etree._Element, header: TsdLisa3Header) -> None:
    """Append ``tsd_L3_0`` — header scalars only (no repeating lists
    modelled; see generator.py's Lisa 3 section docstring)."""
    lisa3 = etree.SubElement(root, TSD_EL_LISA3)
    _append_optional_fields(lisa3, header, TSD_LISA3_ELEMENTS)


def _append_lisa4_block(root: etree._Element, header: TsdLisa4Header) -> None:
    """Append ``tsd_L4_0`` — header scalars only (no repeating lists in
    the XSD for this annex at all)."""
    lisa4 = etree.SubElement(root, TSD_EL_LISA4)
    _append_optional_fields(lisa4, header, TSD_LISA4_ELEMENTS)


def _append_lisa5_block(root: etree._Element, header: TsdLisa5Header) -> None:
    """Append ``tsd_L5_0`` — header scalars only (``tsd_L5_3`` repeating
    list not modelled; see generator.py's Lisa 5 section docstring)."""
    lisa5 = etree.SubElement(root, TSD_EL_LISA5)
    _append_optional_fields(lisa5, header, TSD_LISA5_ELEMENTS)


def _append_lisa6_block(root: etree._Element, listing: TsdLisa6Listing) -> None:
    """Append ``tsd_L6_0`` — header totals + L6_1/L6_2/L6_3 repeating
    lists, XSD order."""
    lisa6 = etree.SubElement(root, TSD_EL_LISA6)
    _append_optional_fields(lisa6, listing.header, TSD_LISA6_HEADER_ELEMENTS)
    if listing.rows1:
        row_list = etree.SubElement(lisa6, TSD_EL_L6_1_LIST)
        for row in listing.rows1:
            node = etree.SubElement(row_list, TSD_EL_L6_1)
            _append_optional_fields(node, row, TSD_LISA6_ROW1_ELEMENTS, int_fields=TSD_LISA6_INT_FIELDS)
    if listing.rows2:
        row_list = etree.SubElement(lisa6, TSD_EL_L6_2_LIST)
        for row in listing.rows2:
            node = etree.SubElement(row_list, TSD_EL_L6_2)
            _append_optional_fields(node, row, TSD_LISA6_ROW2_ELEMENTS)
    if listing.rows3:
        row_list = etree.SubElement(lisa6, TSD_EL_L6_3_LIST)
        for row in listing.rows3:
            node = etree.SubElement(row_list, TSD_EL_L6_3)
            _append_optional_fields(node, row, TSD_LISA6_ROW3_ELEMENTS, int_fields=TSD_LISA6_INT_FIELDS)


def _append_lisa7_block(root: etree._Element, listing: TsdLisa7Listing) -> None:
    """Append ``tsd_L7_0`` — header totals + 1b/1C/2/2B/4 repeating
    lists, XSD order (``tsd_L7_3``/``tsd_L7_5`` not modelled — no source
    row type built for either, see generator.py's Lisa 7 section
    docstring)."""
    lisa7 = etree.SubElement(root, TSD_EL_LISA7)
    _append_optional_fields(lisa7, listing.header, TSD_LISA7_HEADER_ELEMENTS)
    if listing.rows_1b:
        row_list = etree.SubElement(lisa7, TSD_EL_L7_1B_LIST)
        for row in listing.rows_1b:
            node = etree.SubElement(row_list, TSD_EL_L7_1B)
            _append_optional_fields(node, row, TSD_LISA7_ROW1B_ELEMENTS)
    if listing.rows_1c:
        row_list = etree.SubElement(lisa7, TSD_EL_L7_1C_LIST)
        for row in listing.rows_1c:
            node = etree.SubElement(row_list, TSD_EL_L7_1C)
            _append_optional_fields(node, row, TSD_LISA7_ROW1C_ELEMENTS, int_fields=TSD_LISA7_INT_FIELDS)
    if listing.rows_2:
        row_list = etree.SubElement(lisa7, TSD_EL_L7_2_LIST)
        for row in listing.rows_2:
            node = etree.SubElement(row_list, TSD_EL_L7_2)
            _append_optional_fields(node, row, TSD_LISA7_ROW2_ELEMENTS)
    if listing.rows_2b:
        row_list = etree.SubElement(lisa7, TSD_EL_L7_2B_LIST)
        for row in listing.rows_2b:
            node = etree.SubElement(row_list, TSD_EL_L7_2B)
            _append_optional_fields(node, row, TSD_LISA7_ROW2B_ELEMENTS, int_fields=TSD_LISA7_INT_FIELDS)
    if listing.rows_4:
        row_list = etree.SubElement(lisa7, TSD_EL_L7_4_LIST)
        for row in listing.rows_4:
            node = etree.SubElement(row_list, TSD_EL_L7_4)
            _append_optional_fields(node, row, TSD_LISA7_ROW4_ELEMENTS)


@dataclass(frozen=True)
class TsdReportingContext:
    """Filer identity + taxable period. ``year``/``month`` derive from
    ``period_start``; ``load_method`` defaults to "L" (new return)."""

    regcode: str
    period_start: date
    period_end: date
    load_method: str = TSD_LOAD_METHOD_NEW

    @property
    def year(self) -> int:
        return self.period_start.year

    @property
    def month(self) -> int:
        return self.period_start.month


def _build_envelope(ctx: TsdReportingContext) -> etree._Element:
    """The five mandatory ``tsd_vorm`` header elements only (regKood/c108/
    c109/laadimisViis/vorm) — every other top-level child is
    ``minOccurs="0"`` in the XSD, so this alone is already a valid (if
    minimal) document. Shared by ``build_tsd_xml_document`` and the
    standalone ``build_tsd_lisaN_xml_document`` single-annex builders
    below."""
    root = etree.Element(TSD_ROOT_ELEMENT)
    etree.SubElement(root, TSD_EL_REGKOOD).text = ctx.regcode
    etree.SubElement(root, TSD_EL_YEAR).text = str(ctx.year)
    etree.SubElement(root, TSD_EL_MONTH).text = str(ctx.month)
    etree.SubElement(root, TSD_EL_LOAD_METHOD).text = ctx.load_method
    etree.SubElement(root, TSD_EL_FORM).text = TSD_FORM_TSD
    return root


def build_tsd_xml_document(
    listing: TsdListing,
    ctx: TsdReportingContext,
    *,
    lisa2: TsdLisa2Listing | None = None,
    lisa3: TsdLisa3Header | None = None,
    lisa4: TsdLisa4Header | None = None,
    lisa5: TsdLisa5Header | None = None,
    lisa6: TsdLisa6Listing | None = None,
    lisa7: TsdLisa7Listing | None = None,
) -> bytes:
    """Render a TSD return as a ``tsd_vorm`` document.

    Header (regKood/c108/c109/laadimisViis/vorm) then the calculated MAIN
    roll-up (c110/c115/c116/c117) then Lisa 1 (``tsd_L1_0`` -> ``aIsikList`` ->
    one ``tsd_L1_A_Isik`` per person -> ``vmList`` -> one ``tsd_L1_A_Vm`` per
    payment). A zero-row period still emits ``tsd_L1_0`` with an empty
    ``aIsikList``.

    Module 1 (ee-frontier-build-plan.md §"MODULE 1"): ``lisa2``-``lisa7``
    are optional aggregates — when supplied, the corresponding
    ``tsd_L2_0``..``tsd_L7_0`` block is appended after Lisa 1, in XSD
    declaration order (mirrors ``tsdVorm``'s own ``tsd_L1_0`` .. ``tsd_L8_0``
    order — ``xs:all`` makes order technically free, same posture as the
    existing header note above). Callers building ONLY one annex (the
    common case today, since Module 1 ships no generator) can pass a
    zero-row/empty ``TsdListing`` for the MAIN+Lisa1 argument — see
    ``build_tsd_lisaN_xml_document`` below for single-annex convenience
    wrappers that do exactly that."""
    root = _build_envelope(ctx)

    for key, element_name in TSD_MAIN_ELEMENTS:
        etree.SubElement(root, element_name).text = _money(_main_value(listing.main, key))

    lisa1 = etree.SubElement(root, TSD_EL_LISA1)
    isik_list = etree.SubElement(lisa1, TSD_EL_A_ISIK_LIST)
    for isikukood, payments in _group_by_person(listing.lisa1):
        isik = etree.SubElement(isik_list, TSD_EL_A_ISIK)
        etree.SubElement(isik, TSD_EL_ISIK_KOOD).text = isikukood
        vm_list = etree.SubElement(isik, TSD_EL_VM_LIST)
        for row in payments:
            vm = etree.SubElement(vm_list, TSD_EL_VM)
            for key, element_name in TSD_VM_ELEMENTS:
                etree.SubElement(vm, element_name).text = _xml_text(_vm_value(row, key))

    if lisa2 is not None:
        _append_lisa2_block(root, lisa2)
    if lisa3 is not None:
        _append_lisa3_block(root, lisa3)
    if lisa4 is not None:
        _append_lisa4_block(root, lisa4)
    if lisa5 is not None:
        _append_lisa5_block(root, lisa5)
    if lisa6 is not None:
        _append_lisa6_block(root, lisa6)
    if lisa7 is not None:
        _append_lisa7_block(root, lisa7)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ---- Single-annex convenience wrappers (Module 1) --------------------------
# Each builds a MINIMAL valid ``tsd_vorm`` document — envelope + exactly one
# Lisa 2-7 block, no MAIN/Lisa-1 (both ``minOccurs=0``, safe to omit) — for
# testing/XSD-validating one annex in isolation, without needing a full
# ``TsdListing``.

def build_tsd_lisa2_xml_document(listing: TsdLisa2Listing, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa2_block(root, listing)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_lisa3_xml_document(header: TsdLisa3Header, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa3_block(root, header)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_lisa4_xml_document(header: TsdLisa4Header, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa4_block(root, header)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_lisa5_xml_document(header: TsdLisa5Header, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa5_block(root, header)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_lisa6_xml_document(listing: TsdLisa6Listing, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa6_block(root, listing)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_lisa7_xml_document(listing: TsdLisa7Listing, ctx: TsdReportingContext) -> bytes:
    root = _build_envelope(ctx)
    _append_lisa7_block(root, listing)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _csv_money(value: Decimal) -> str:
    """2 dp with a COMMA decimal separator (TSD CSV spec)."""
    return _money(value).replace(".", ",")


def _csv_text(value: str) -> str:
    """Quote a text field (chr 34), escaping ``\\`` then ``"`` per the spec."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Which Lisa-1 CSV column codes are text (quoted) vs numeric (comma decimal).
_TSD_TEXT_CODES = frozenset({"1000", "1020"})


def _tsd_csv(header_codes: list[str], data_rows: list[list[str]]) -> bytes:
    """Assemble a TSD CSV: UTF-8 with BOM, CRLF lines, ';' separator, no
    trailing ';'. Cells are pre-formatted (quoting/decimal handled by caller)."""
    lines = [TSD_CSV_DELIMITER.join(header_codes)]
    lines += [TSD_CSV_DELIMITER.join(cells) for cells in data_rows]
    return (TSD_CSV_BOM + "\r\n".join(lines) + "\r\n").encode(TSD_CSV_ENCODING)


def build_tsd_lisa1_csv_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """Lisa-1 CSV (Annex 1 subform 1a): header row of column codes then one row
    per payment. Text fields quoted, money with a comma decimal."""
    header = [code for _, code in TSD_LISA1_CSV_COLUMNS]
    data_rows: list[list[str]] = []
    for row in listing.lisa1:
        cells: list[str] = []
        for key, code in TSD_LISA1_CSV_COLUMNS:
            value = _vm_value(row, key) if key == "payment_type_code" else getattr(row, key)
            if code in _TSD_TEXT_CODES:
                cells.append(_csv_text(str(value)))
            else:
                cells.append(_csv_money(value))
        data_rows.append(cells)
    return _tsd_csv(header, data_rows)


def build_tsd_main_csv_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """MAIN roll-up CSV: header row of main-form codes then one value row. The
    official CSV spec is annex-focused; this follows the same code-header style
    (year/month plain integers, money with a comma decimal)."""
    header = [_code for _, _code in TSD_MAIN_CSV_COLUMNS]
    cells: list[str] = []
    for key, _code in TSD_MAIN_CSV_COLUMNS:
        if key == "_year":
            cells.append(str(ctx.year))
        elif key == "_month":
            cells.append(str(ctx.month))
        else:
            cells.append(_csv_money(_main_value(listing.main, key)))
    return _tsd_csv(header, [cells])


def build_tsd_lisa2_a_csv_document(a_rows: list[TsdLisa2ARow]) -> bytes:
    """Lisa 2 subform A CSV ("Annex2 subform 1a" in
    ``csv_tsd_failiformaadid_01.01.2025_eng.pdf``) — the ONLY Lisa 2-7
    subform with a real, pinned CSV column table (see mapping.py's Module
    1 section docstring for why every other Lisa 2-7 subform is XML-only).
    One row per ``TsdLisa2ARow`` (already flat — no grouping needed for a
    CSV export, unlike the XML's person-grouped nesting)."""
    header = [code for _, code in TSD_LISA2_A_CSV_COLUMNS]
    data_rows: list[list[str]] = []
    for row in a_rows:
        cells: list[str] = []
        for key, code in TSD_LISA2_A_CSV_COLUMNS:
            value = getattr(row, key)
            if code in TSD_LISA2_A_CSV_TEXT_CODES:
                cells.append(_csv_text("" if value is None else str(value)))
            elif value is None:
                cells.append("")
            else:
                cells.append(_csv_money(value))
        data_rows.append(cells)
    return _tsd_csv(header, data_rows)


def _to_jsonable(value: Any) -> Any:
    """Convert one non-container value to something ``json``/JSONB can
    hold natively, preserving ``Decimal`` precision as a string (JSONB
    has no decimal type — same rationale as
    ``tax_return_generator.persist_return``'s ``str(box.amount)``)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _mask_isikukood(plain: str) -> str:
    """Mask an Estonian isikukood for persistence (critic round 2
    finding). The sensitive part is the FIRST 7 digits (century/sex
    digit + YYMMDD birthdate) — unlike ``services.employees._mask_tfn``
    (an opaque AU number where showing the tail is safe), showing an
    isikukood's tail and hiding its head is what actually protects the
    DOB/sex it encodes. Masks the first 7 characters, keeps the
    trailing serial+checksum digits (or all of it, if shorter than 7 —
    defensive, should not happen for a real isikukood)."""
    if len(plain) <= 7:
        return "X" * len(plain)
    return ("X" * 7) + plain[7:]


def _asdict_jsonable(obj: TsdMainTotals | TsdLisa1Row | TsdDataQualityError) -> dict[str, Any]:
    """``dataclasses.asdict`` + JSONB-safe value conversion, with one
    field-specific redaction: ``TsdLisa1Row.isikukood`` is masked before
    it lands in ``tax_returns.figures`` (critic round 2 finding —
    ``persist_tsd_return`` was writing the FULL plaintext isikukood into
    a plain JSONB column with no field-level encryption, then the
    generic ``GET /api/v1/tax_returns`` routes returned it verbatim to
    any bearer-authenticated caller in the tenant, with no
    ``employee.tfn_view``-style gate). ``employee_id`` already gives a
    persisted row's full audit-provenance trail (module docstring), so
    the plaintext isikukood is not needed in the JSONB copy — it stays
    plaintext ONLY in the actual filed XML/CSV export (``build_tsd_*``
    above), its one legitimate destination."""
    data = {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, TsdLisa1Row):
        data["isikukood"] = _mask_isikukood(obj.isikukood)
    return data


async def persist_tsd_return(
    session: AsyncSession,
    listing: TsdListing,
    *,
    tenant_id: uuid.UUID,
    period_id: uuid.UUID,
    status: TaxReturnStatus = TaxReturnStatus.READY,
    generated_by_user_id: uuid.UUID | None = None,
) -> TaxReturn:
    """Persist a computed ``TsdListing`` to ``tax_returns`` (company DB),
    ``return_type="TSD"``.

    Scope §3.3's decision, made here: reuse ``tax_returns`` (the KMD
    pattern via ``tax_return_generator.persist_return``) rather than a new
    table (no new company-DB table — RLS checklist not triggered, per the
    scope), but with a **list-shaped** ``figures`` payload instead of that
    function's flat ``box_code -> {amount,...}`` shape, since TSD is a
    repeating-row annex, not a box vector: ``{"main": {...9-ish fields},
    "lisa1": [...full row per person-payment, INCLUDING employee_id /
    pay_run_id for audit provenance...], "errors": [...surfaced
    data-quality errors, so a persisted TSD return does not silently
    forget which lines were excluded...]}``.

    Does not commit — caller controls the transaction boundary (mirrors
    ``persist_return``)."""
    figures: dict[str, Any] = {
        "main": _asdict_jsonable(listing.main),
        "lisa1": [_asdict_jsonable(row) for row in listing.lisa1],
        "errors": [_asdict_jsonable(err) for err in listing.errors],
        # Critic round 2 finding — see TsdListing.gl_not_posted_pay_run_ids'
        # docstring: separate from "errors" (listing-wide provenance
        # fact, not a per-line exclusion) so a reviewer of the persisted
        # return can see these totals have no GL posting behind them yet.
        "gl_not_posted_pay_run_ids": [
            str(pr_id) for pr_id in listing.gl_not_posted_pay_run_ids
        ],
    }
    row = TaxReturn(
        company_id=listing.company_id,
        tenant_id=tenant_id,
        jurisdiction="EE",
        period_id=period_id,
        return_type="TSD",
        figures=figures,
        status=status,
        generated_by_user_id=generated_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row
