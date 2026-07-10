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
    TSD_ATTR_PERIOD_END,
    TSD_ATTR_PERIOD_START,
    TSD_ATTR_REGCODE,
    TSD_CSV_DELIMITER,
    TSD_CSV_ENCODING,
    TSD_CSV_HEADER_PERIOD_END,
    TSD_CSV_HEADER_PERIOD_START,
    TSD_CSV_HEADER_REGCODE,
    TSD_LISA1_COLUMNS,
    TSD_LISA1_CONTAINER_ELEMENT,
    TSD_LISA1_FIELD_NAMES,
    TSD_LISA1_ROW_ELEMENT,
    TSD_MAIN_COLUMNS,
    TSD_MAIN_ELEMENT,
    TSD_MAIN_FIELD_NAMES,
    TSD_ROOT_ELEMENT,
    TSD_SCHEMA_REF,
    TSD_TAXONOMY_NS,
    TSD_TAXONOMY_PREFIX,
)

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal) -> str:
    """Cent-precision string, 2 dp, ROUND_HALF_UP — same convention as
    ``kmd.serializer._money`` / ``kmd_inf.serializer._money`` (deliberately
    reused, not re-derived)."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _format_value(value: Any) -> str:
    """Type-driven cell/text formatter for a field value — dispatch on
    type rather than field name, same posture as
    ``kmd_inf.serializer._format_value`` (shared by MAIN and Lisa 1)."""
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
class TsdReportingContext:
    """The filer identity + period every TSD file carries. Kept as a
    separate type (not imported from ``kmd``/``kmd_inf``) so ``tsd`` stays
    a self-contained sibling package, per the scope's package layout.
    ``@dataclass(frozen=True)``, mirroring both siblings'
    ``KmdReportingContext``/``KmdInfReportingContext`` exactly."""

    regcode: str
    period_start: date
    period_end: date


def build_tsd_xml_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """Render a TSD return (MAIN + Lisa 1) as one XML document.

    MAIN always emits all 7 aggregate fields explicitly (a reported nil
    is not an absent field — mirrors ``kmd.build_kmd_xml_document``'s
    convention for its flat vector). Lisa 1 emits an EMPTY container for
    zero qualifying rows, not a placeholder row (mirrors
    ``kmd_inf.build_kmd_inf_xml_document``'s "N rows, N may be 0")."""
    nsmap = {TSD_TAXONOMY_PREFIX: TSD_TAXONOMY_NS}
    root = etree.Element(etree.QName(TSD_TAXONOMY_NS, TSD_ROOT_ELEMENT), nsmap=nsmap)
    root.set("schemaRef", TSD_SCHEMA_REF)
    root.set(TSD_ATTR_REGCODE, ctx.regcode)
    root.set(TSD_ATTR_PERIOD_START, ctx.period_start.isoformat())
    root.set(TSD_ATTR_PERIOD_END, ctx.period_end.isoformat())

    main_el = etree.SubElement(root, etree.QName(TSD_TAXONOMY_NS, TSD_MAIN_ELEMENT))
    for key in TSD_MAIN_COLUMNS:
        sub = etree.SubElement(main_el, etree.QName(TSD_TAXONOMY_NS, TSD_MAIN_FIELD_NAMES[key]))
        sub.text = _format_value(getattr(listing.main, key))

    lisa1_el = etree.SubElement(root, etree.QName(TSD_TAXONOMY_NS, TSD_LISA1_CONTAINER_ELEMENT))
    for row in listing.lisa1:
        row_el = etree.SubElement(lisa1_el, etree.QName(TSD_TAXONOMY_NS, TSD_LISA1_ROW_ELEMENT))
        for key in TSD_LISA1_COLUMNS:
            sub = etree.SubElement(row_el, etree.QName(TSD_TAXONOMY_NS, TSD_LISA1_FIELD_NAMES[key]))
            sub.text = _format_value(getattr(row, key))

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_tsd_main_csv_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """MAIN aggregate CSV — one header row + one data row (a single
    period's totals, not a per-line listing — mirrors
    ``kmd.build_kmd_csv_document``'s single-row shape)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=TSD_CSV_DELIMITER, lineterminator="\r\n")
    header = [
        TSD_CSV_HEADER_REGCODE,
        TSD_CSV_HEADER_PERIOD_START,
        TSD_CSV_HEADER_PERIOD_END,
        *(TSD_MAIN_FIELD_NAMES[key] for key in TSD_MAIN_COLUMNS),
    ]
    row = [
        ctx.regcode,
        ctx.period_start.isoformat(),
        ctx.period_end.isoformat(),
        *(_format_value(getattr(listing.main, key)) for key in TSD_MAIN_COLUMNS),
    ]
    writer.writerow(header)
    writer.writerow(row)
    return buf.getvalue().encode(TSD_CSV_ENCODING)


def build_tsd_lisa1_csv_document(listing: TsdListing, ctx: TsdReportingContext) -> bytes:
    """Lisa-1 CSV — one header row + N data rows (genuinely multi-row,
    mirrors ``kmd_inf``'s Part A/B CSVs). Each data row repeats the header
    regcode/period as its leading three columns (PLACEHOLDER convention,
    see ``mapping.py``), same self-describing-row posture as
    ``kmd_inf.serializer._build_csv``."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=TSD_CSV_DELIMITER, lineterminator="\r\n")
    header = [
        TSD_CSV_HEADER_REGCODE,
        TSD_CSV_HEADER_PERIOD_START,
        TSD_CSV_HEADER_PERIOD_END,
        *(TSD_LISA1_FIELD_NAMES[key] for key in TSD_LISA1_COLUMNS),
    ]
    writer.writerow(header)
    for row in listing.lisa1:
        writer.writerow([
            ctx.regcode,
            ctx.period_start.isoformat(),
            ctx.period_end.isoformat(),
            *(_format_value(getattr(row, key)) for key in TSD_LISA1_COLUMNS),
        ])
    return buf.getvalue().encode(TSD_CSV_ENCODING)


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
