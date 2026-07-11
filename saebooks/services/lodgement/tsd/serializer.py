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

import csv
import io
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
    TsdListing,
    TsdMainTotals,
)
from saebooks.services.lodgement.tsd.mapping import (
    TSD_CSV_DELIMITER,
    TSD_CSV_ENCODING,
    TSD_CSV_BOM,
    TSD_EL_A_ISIK,
    TSD_EL_A_ISIK_LIST,
    TSD_EL_FORM,
    TSD_EL_ISIK_KOOD,
    TSD_EL_LISA1,
    TSD_EL_LOAD_METHOD,
    TSD_EL_MONTH,
    TSD_EL_REGKOOD,
    TSD_EL_VM,
    TSD_EL_VM_LIST,
    TSD_EL_YEAR,
    TSD_FORM_TSD,
    TSD_LISA1_CSV_COLUMNS,
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


def build_tsd_xml_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """Render a TSD return as a ``tsd_vorm`` document.

    Header (regKood/c108/c109/laadimisViis/vorm) then the calculated MAIN
    roll-up (c110/c115/c116/c117) then Lisa 1 (``tsd_L1_0`` -> ``aIsikList`` ->
    one ``tsd_L1_A_Isik`` per person -> ``vmList`` -> one ``tsd_L1_A_Vm`` per
    payment). A zero-row period still emits ``tsd_L1_0`` with an empty
    ``aIsikList``."""
    root = etree.Element(TSD_ROOT_ELEMENT)
    etree.SubElement(root, TSD_EL_REGKOOD).text = ctx.regcode
    etree.SubElement(root, TSD_EL_YEAR).text = str(ctx.year)
    etree.SubElement(root, TSD_EL_MONTH).text = str(ctx.month)
    etree.SubElement(root, TSD_EL_LOAD_METHOD).text = ctx.load_method
    etree.SubElement(root, TSD_EL_FORM).text = TSD_FORM_TSD

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
    header = [code for _, code in TSD_MAIN_CSV_COLUMNS]
    cells: list[str] = []
    for key, code in TSD_MAIN_CSV_COLUMNS:
        if key == "_year":
            cells.append(str(ctx.year))
        elif key == "_month":
            cells.append(str(ctx.month))
        else:
            cells.append(_csv_money(_main_value(listing.main, key)))
    return _tsd_csv(header, [cells])


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
