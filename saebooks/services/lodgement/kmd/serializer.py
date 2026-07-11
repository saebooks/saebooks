"""EE KMD (VAT return) file serializer — official e-MTA ``vatDeclaration``.

Renders a computed KMD box vector to the real e-MTA ``vatDeclaration`` document
(XML primary, CSV secondary) for the manual-upload / X-Road file path. Element
names are now PINNED to the real schema — see ``mapping.py``.

Combined vs separate parts (format-PDF decision)
------------------------------------------------
A ``vatDeclaration`` contains three parts — ``declarationBody`` (KMD main form),
``salesAnnex`` (KMD-INF Part A) and ``purchasesAnnex`` (KMD-INF Part B) — which
"may be transmitted separately". This module provides ONE builder,
``build_vat_declaration_document``, that emits any combination:

  * ``figures`` only                 -> declarationBody-only document (KMD alone)
  * ``sales_annex``/``purchases_annex`` only -> annex-only document (KMD-INF alone)
  * both                             -> the full combined vatDeclaration

The public ``build_kmd_xml_document`` is a thin wrapper (body only), preserved so
existing callers/tests don't break. ``kmd_inf.serializer`` builds the annex
elements and calls ``build_vat_declaration_document`` for the annex-only /
combined cases (dependency direction kmd_inf -> kmd; kmd never imports kmd_inf).

Rounding: euros to the cent (2 dp), ROUND_HALF_UP (MonetaryValue = xs:decimal
fractionDigits=2).
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
    KMD_BODY_ELEMENTS,
    KMD_CSV_BODY_COLUMNS,
    KMD_CSV_DELIMITER,
    KMD_CSV_ENCODING,
    KMD_CSV_HEADER_SYMBOL,
    KMD_EL_DECLARATION_BODY,
    KMD_EL_DECLARATION_TYPE,
    KMD_EL_MONTH,
    KMD_EL_NO_PURCHASES,
    KMD_EL_NO_SALES,
    KMD_EL_SUBMITTER_PERSON_CODE,
    KMD_EL_SUM_PER_PARTNER_PURCHASES,
    KMD_EL_SUM_PER_PARTNER_SALES,
    KMD_EL_TAXPAYER_REGCODE,
    KMD_EL_VERSION,
    KMD_EL_YEAR,
    KMD_EMITTED_BOX_ORDER,
    KMD_FIELD_NAMES,
    KMD_ROOT_ELEMENT,
    resolve_kmd_version,
)

_TWO_PLACES = Decimal("0.01")


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    """Cent-precision string, 2 dp, ROUND_HALF_UP."""
    return str(_dec(value).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _flag(value: bool) -> str:
    """xs:boolean canonical lexical form."""
    return "true" if value else "false"


@dataclass(frozen=True)
class KmdReportingContext:
    """The filer identity + taxable period every KMD file carries.

    Backward-compatible: ``regcode``/``period_start``/``period_end`` are the
    only required fields (existing callers unchanged). ``year``/``month``/
    ``version`` are DERIVED from ``period_start`` unless overridden. The four
    declarationBody flags default to a "there are transactions" posture
    (``no_sales``/``no_purchases`` False; per-partner-summing False)."""

    regcode: str
    period_start: date
    period_end: date
    submitter_person_code: str | None = None
    declaration_type: str = "1"  # 1 = normal period, 2 = bankruptcy
    version: str | None = None   # None -> resolve from period
    no_sales: bool = False
    no_purchases: bool = False
    sum_per_partner_sales: bool = False
    sum_per_partner_purchases: bool = False

    @property
    def year(self) -> int:
        return self.period_start.year

    @property
    def month(self) -> int:
        return self.period_start.month

    def resolved_version(self) -> str:
        return self.version or resolve_kmd_version(self.year, self.month)


@dataclass(frozen=True)
class KmdFigures:
    """Normalised KMD box vector — the serializer's stable input contract.

    Keyed by the engine box-code convention (``"1"``, ``"1-1"``, ``"3.1.1"``,
    ...). Only ``mapping.KMD_EMITTED_BOX_ORDER`` boxes are serialized; computed
    boxes (4, 4-1, 12, 13) may be present but are ignored (e-MTA derives them)."""

    boxes: dict[str, Decimal] = field(default_factory=dict)

    def amount(self, box_code: str) -> Decimal:
        return self.boxes.get(box_code, Decimal("0"))

    @classmethod
    def from_box_amounts(cls, amounts: dict[str, Any]) -> KmdFigures:
        """Keep only the filable boxes (``KMD_EMITTED_BOX_ORDER``) — the seed's
        internal helper boxes (``1_DOMESTIC``/``1_RC``/...) and the e-MTA-computed
        boxes (4, 4-1, 12, 13) are ignored."""
        return cls(boxes={code: _dec(amounts.get(code, 0)) for code in KMD_EMITTED_BOX_ORDER})

    @classmethod
    def from_tax_return_result(cls, result: Any) -> KmdFigures:
        return cls.from_box_amounts({code: b.amount for code, b in result.boxes.items()})

    @classmethod
    def from_figures_json(cls, figures: dict[str, Any]) -> KmdFigures:
        """Build from a ``tax_returns.figures`` JSONB dict. Exact box_code key
        match only (KMD codes collide under separator-stripping: ``"1-1"`` ->
        ``"11"`` clashes with real box ``"11"``)."""
        boxes: dict[str, Decimal] = {}
        for code in KMD_EMITTED_BOX_ORDER:
            val = figures.get(code)
            if isinstance(val, dict) and "amount" in val:
                val = val["amount"]
            boxes[code] = _dec(val) if val is not None else Decimal("0")
        return cls(boxes=boxes)


def _declaration_body_element(figures: KmdFigures, ctx: KmdReportingContext) -> etree._Element:
    """Build a ``<declarationBody>`` element: the four mandatory flags then the
    24 filable monetary boxes in KMD6 order (every box emitted, even 0.00)."""
    body = etree.Element(KMD_EL_DECLARATION_BODY)
    etree.SubElement(body, KMD_EL_NO_SALES).text = _flag(ctx.no_sales)
    etree.SubElement(body, KMD_EL_NO_PURCHASES).text = _flag(ctx.no_purchases)
    etree.SubElement(body, KMD_EL_SUM_PER_PARTNER_SALES).text = _flag(ctx.sum_per_partner_sales)
    etree.SubElement(body, KMD_EL_SUM_PER_PARTNER_PURCHASES).text = _flag(ctx.sum_per_partner_purchases)
    for box_code, element_name in KMD_BODY_ELEMENTS:
        etree.SubElement(body, element_name).text = _money(figures.amount(box_code))
    return body


def build_vat_declaration_document(
    ctx: KmdReportingContext,
    *,
    figures: KmdFigures | None = None,
    sales_annex: etree._Element | None = None,
    purchases_annex: etree._Element | None = None,
) -> bytes:
    """Render a ``vatDeclaration`` document — any combination of the three parts.

    ``figures`` -> ``declarationBody``; ``sales_annex``/``purchases_annex`` are
    pre-built ``<salesAnnex>``/``<purchasesAnnex>`` elements (from
    ``kmd_inf.serializer``). Envelope element order per the KMD6 example:
    taxPayerRegCode, submitterPersonCode?, year, month, declarationType, version,
    declarationBody?, salesAnnex*, purchasesAnnex*."""
    root = etree.Element(KMD_ROOT_ELEMENT)
    etree.SubElement(root, KMD_EL_TAXPAYER_REGCODE).text = ctx.regcode
    if ctx.submitter_person_code:
        etree.SubElement(root, KMD_EL_SUBMITTER_PERSON_CODE).text = ctx.submitter_person_code
    etree.SubElement(root, KMD_EL_YEAR).text = str(ctx.year)
    etree.SubElement(root, KMD_EL_MONTH).text = f"{ctx.month:02d}"
    etree.SubElement(root, KMD_EL_DECLARATION_TYPE).text = ctx.declaration_type
    etree.SubElement(root, KMD_EL_VERSION).text = ctx.resolved_version()
    if figures is not None:
        root.append(_declaration_body_element(figures, ctx))
    if sales_annex is not None:
        root.append(sales_annex)
    if purchases_annex is not None:
        root.append(purchases_annex)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_kmd_xml_document(figures: KmdFigures, ctx: KmdReportingContext) -> bytes:
    """KMD main form alone (declarationBody-only ``vatDeclaration``) — thin
    wrapper over ``build_vat_declaration_document``, preserved for callers."""
    return build_vat_declaration_document(ctx, figures=figures)


def build_kmd_csv_document(figures: KmdFigures, ctx: KmdReportingContext) -> bytes:
    """KMD main form as a row-symbol CSV row (``KMD5``/``KMD6``;...).

    One body row: ``<version>;noSales;noPurchases;sumPerPartnerSales;
    sumPerPartnerPurchases;<24 boxes in KMD6 order>``. No column-name header row
    (the row symbol identifies the row); no trailing ';'. The M2M-only ``header``
    row is omitted for the manual-upload path. Column order == XML element order
    (CSV-format rule 11)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=KMD_CSV_DELIMITER, lineterminator="\r\n")
    row = [
        ctx.resolved_version(),
        _flag(ctx.no_sales),
        _flag(ctx.no_purchases),
        _flag(ctx.sum_per_partner_sales),
        _flag(ctx.sum_per_partner_purchases),
        *(
            "" if kind == "empty" else _money(figures.amount(key))
            for kind, key in KMD_CSV_BODY_COLUMNS
        ),
    ]
    writer.writerow(row)
    return buf.getvalue().encode(KMD_CSV_ENCODING)
