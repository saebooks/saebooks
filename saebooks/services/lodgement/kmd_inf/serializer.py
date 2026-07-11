"""KMD-INF (VAT-return invoice annex) serializer — official e-MTA
``salesAnnex`` / ``purchasesAnnex``.

KMD-INF is not a standalone document: Part A = ``salesAnnex`` and Part B =
``purchasesAnnex``, both children of the same ``vatDeclaration`` the KMD main
form lives in. This module builds those annex ELEMENTS from the generator's
``KmdInfListing`` and hands them to ``kmd.serializer.build_vat_declaration_document``
(dependency direction kmd_inf -> kmd). Element names are PINNED — see
``mapping.py``.

Public builders (preserved): ``build_kmd_inf_xml_document`` (annex-only
vatDeclaration, no declarationBody) and the two CSV builders. Rounding: euros /
tax to 2 dp ROUND_HALF_UP.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lxml import etree

from saebooks.services.lodgement.kmd.serializer import (
    KmdReportingContext,
    build_vat_declaration_document,
)
from saebooks.services.lodgement.kmd_inf.generator import (
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
)
from saebooks.services.lodgement.kmd_inf.mapping import (
    KMD_INF_CSV_DELIMITER,
    KMD_INF_CSV_ENCODING,
    KMD_INF_CSV_PURCHASES_SYMBOL,
    KMD_INF_CSV_SALES_SYMBOL,
    KMD_INF_EL_NO_PURCHASES,
    KMD_INF_EL_NO_SALES,
    KMD_INF_EL_PURCHASE_LINE,
    KMD_INF_EL_PURCHASES_ANNEX,
    KMD_INF_EL_SALE_LINE,
    KMD_INF_EL_SALES_ANNEX,
    KMD_INF_EL_SUM_PER_PARTNER_PURCHASES,
    KMD_INF_EL_SUM_PER_PARTNER_SALES,
    KMD_INF_PART_A_ELEMENTS,
    KMD_INF_PART_B_ELEMENTS,
    tax_rate_classifier,
)

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal) -> str:
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _format_value(value: Any) -> str:
    """Type-driven cell/text formatter — None -> "" (empty element/cell)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# Synthetic column keys (mapping.py "_"-prefixed) resolved here — everything
# else is a plain row attribute.
def _part_a_value(row: KmdInfPartARow, key: str) -> Any:
    if key == "_tax_rate_classifier":
        return tax_rate_classifier(row.rate)
    if key == "_invoice_sum_for_rate":
        return row.taxable_value      # cell 8 (UNVERIFIED, = taxable_value)
    if key == "_sum_for_rate_in_period":
        return row.taxable_value      # cell 9 (period-declared taxable value)
    return getattr(row, key)


def _part_b_value(row: KmdInfPartBRow, key: str) -> Any:
    if key == "_vat_sum":
        return row.input_vat          # cell 7 (UNVERIFIED, = input_vat)
    return getattr(row, key)


@dataclass(frozen=True)
class KmdInfReportingContext:
    """Filer identity + period. Mirrors ``KmdReportingContext``'s required
    fields; kept as a separate type so ``kmd_inf`` stays a self-contained
    sibling. Converted to a ``KmdReportingContext`` for the shared envelope."""

    regcode: str
    period_start: date
    period_end: date
    submitter_person_code: str | None = None
    declaration_type: str = "1"
    version: str | None = None

    def _kmd_ctx(self, *, no_sales: bool, no_purchases: bool) -> KmdReportingContext:
        return KmdReportingContext(
            regcode=self.regcode,
            period_start=self.period_start,
            period_end=self.period_end,
            submitter_person_code=self.submitter_person_code,
            declaration_type=self.declaration_type,
            version=self.version,
            no_sales=no_sales,
            no_purchases=no_purchases,
        )


def _sales_annex_element(listing: KmdInfListing) -> etree._Element:
    """Build a ``<salesAnnex>``: noSales, sumPerPartnerSales, then a saleLine
    per Part A row (empty container when there are zero rows)."""
    annex = etree.Element(KMD_INF_EL_SALES_ANNEX)
    etree.SubElement(annex, KMD_INF_EL_NO_SALES).text = "true" if not listing.part_a else "false"
    etree.SubElement(annex, KMD_INF_EL_SUM_PER_PARTNER_SALES).text = "false"
    for row in listing.part_a:
        line = etree.SubElement(annex, KMD_INF_EL_SALE_LINE)
        for key, element_name in KMD_INF_PART_A_ELEMENTS:
            etree.SubElement(line, element_name).text = _format_value(_part_a_value(row, key))
    return annex


def _purchases_annex_element(listing: KmdInfListing) -> etree._Element:
    """Build a ``<purchasesAnnex>``: noPurchases, sumPerPartnerPurchases, then a
    purchaseLine per Part B row."""
    annex = etree.Element(KMD_INF_EL_PURCHASES_ANNEX)
    etree.SubElement(annex, KMD_INF_EL_NO_PURCHASES).text = "true" if not listing.part_b else "false"
    etree.SubElement(annex, KMD_INF_EL_SUM_PER_PARTNER_PURCHASES).text = "false"
    for row in listing.part_b:
        line = etree.SubElement(annex, KMD_INF_EL_PURCHASE_LINE)
        for key, element_name in KMD_INF_PART_B_ELEMENTS:
            etree.SubElement(line, element_name).text = _format_value(_part_b_value(row, key))
    return annex


def build_kmd_inf_xml_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Render a KMD-INF listing as an annex-only ``vatDeclaration`` (both
    ``salesAnnex`` and ``purchasesAnnex``, no ``declarationBody``)."""
    kmd_ctx = ctx._kmd_ctx(no_sales=not listing.part_a, no_purchases=not listing.part_b)
    return build_vat_declaration_document(
        kmd_ctx,
        sales_annex=_sales_annex_element(listing),
        purchases_annex=_purchases_annex_element(listing),
    )


def _build_csv(rows: list, symbol: str, value_fn, elements: tuple[tuple[str, str], ...]) -> bytes:
    """One ``<symbol>;...`` row per line; no column-name header row (the symbol
    identifies the row), no trailing ';'."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=KMD_INF_CSV_DELIMITER, lineterminator="\r\n")
    for row in rows:
        writer.writerow([symbol, *(_format_value(value_fn(row, key)) for key, _ in elements)])
    return buf.getvalue().encode(KMD_INF_CSV_ENCODING)


def build_kmd_inf_part_a_csv_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Part A rows as ``A;buyerRegCode;buyerName;invoiceNumber;invoiceDate;
    invoiceSum;taxRate;invoiceSumForRate;sumForRateInPeriod;comments``."""
    return _build_csv(listing.part_a, KMD_INF_CSV_SALES_SYMBOL, _part_a_value, KMD_INF_PART_A_ELEMENTS)


def build_kmd_inf_part_b_csv_document(listing: KmdInfListing, ctx: KmdInfReportingContext) -> bytes:
    """Part B rows as ``B;sellerRegCode;sellerName;invoiceNumber;invoiceDate;
    invoiceSumVat;vatSum;vatInPeriod;comments``."""
    return _build_csv(listing.part_b, KMD_INF_CSV_PURCHASES_SYMBOL, _part_b_value, KMD_INF_PART_B_ELEMENTS)
