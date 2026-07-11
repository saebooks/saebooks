"""Unit tests for the EE KMD-INF file serializer (pure — no DB).

Pinned to the real e-MTA ``salesAnnex`` / ``purchasesAnnex`` (part of
``vatDeclaration``) — see ``saebooks/services/lodgement/kmd_inf/mapping.py``.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.kmd_inf import (
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
    KmdInfReportingContext,
    build_kmd_inf_part_a_csv_document,
    build_kmd_inf_part_b_csv_document,
    build_kmd_inf_xml_document,
)

_D = Decimal


def _ctx() -> KmdInfReportingContext:
    return KmdInfReportingContext(
        regcode="10123456", period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )


def _row_a(*, credit_note: bool = False) -> KmdInfPartARow:
    sign = _D("-1") if credit_note else _D("1")
    return KmdInfPartARow(
        row_no=1, partner_registration_number="10111111", partner_name="P1 Straddles Both Ways",
        document_number="INV-1", document_date=date(2026, 2, 5),
        document_total_ex_vat=_D("700.00") * sign, taxable_value=_D("700.00") * sign,
        rate=_D("24.000"), kmd_box_code="1", erisuse_kood=None, is_credit_note=credit_note,
    )


def _row_b() -> KmdInfPartBRow:
    return KmdInfPartBRow(
        row_no=1, partner_registration_number="10555555", partner_name="S1 Deductible Supplier",
        document_number="BILL-1", document_date=date(2026, 2, 9),
        document_total_incl_vat=_D("1364.00"), input_vat=_D("264.00"),
        rate=_D("24.000"), erisuse_kood="12",
    )


def _listing(part_a: list[KmdInfPartARow], part_b: list[KmdInfPartBRow]) -> KmdInfListing:
    return KmdInfListing(
        company_id=uuid.uuid4(), period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
        part_a=part_a, part_b=part_b,
    )


def _sale_lines(root: etree._Element) -> list[etree._Element]:
    return root.find("salesAnnex").findall("saleLine")


def _purchase_lines(root: etree._Element) -> list[etree._Element]:
    return root.find("purchasesAnnex").findall("purchaseLine")


def test_kmd_inf_xml_is_an_annex_only_vatdeclaration() -> None:
    root = etree.fromstring(build_kmd_inf_xml_document(_listing([], []), _ctx()))
    assert root.tag == "vatDeclaration"
    assert root.find("taxPayerRegCode").text == "10123456"
    assert root.find("year").text == "2026"
    assert root.find("month").text == "02"
    # annex-only: no declarationBody
    assert root.find("declarationBody") is None
    assert root.find("salesAnnex") is not None
    assert root.find("purchasesAnnex") is not None


def test_kmd_inf_xml_empty_listing_has_no_line_rows() -> None:
    root = etree.fromstring(build_kmd_inf_xml_document(_listing([], []), _ctx()))
    assert _sale_lines(root) == []
    assert _purchase_lines(root) == []
    # empty annex still carries its mandatory noSales flag = true.
    assert root.find("salesAnnex").find("noSales").text == "true"


def test_kmd_inf_xml_part_a_saleline_fields() -> None:
    root = etree.fromstring(build_kmd_inf_xml_document(_listing([_row_a()], []), _ctx()))
    lines = _sale_lines(root)
    assert len(lines) == 1
    line = lines[0]
    assert line.find("buyerRegCode").text == "10111111"
    assert line.find("buyerName").text == "P1 Straddles Both Ways"
    assert line.find("invoiceNumber").text == "INV-1"
    assert line.find("invoiceSum").text == "700.00"
    assert line.find("taxRate").text == "24"  # TAX_RATE_SALES classifier, not "24.00"
    assert line.find("invoiceSumForRate").text == "700.00"
    assert line.find("sumForRateInPeriod").text == "700.00"
    # no erisuse-kood -> empty comments element (lxml normalises "" to None)
    assert line.find("comments").text is None
    # the real saleLine has NO JrkNr / kmd-box / credit-note flag elements
    assert line.find("KreeditArve") is None
    assert line.find("JrkNr") is None


def test_kmd_inf_xml_credit_note_is_signed_negative() -> None:
    root = etree.fromstring(build_kmd_inf_xml_document(_listing([_row_a(credit_note=True)], []), _ctx()))
    line = _sale_lines(root)[0]
    assert line.find("invoiceSum").text == "-700.00"
    assert line.find("invoiceSumForRate").text == "-700.00"


def test_kmd_inf_xml_part_b_purchaseline_fields() -> None:
    root = etree.fromstring(build_kmd_inf_xml_document(_listing([], [_row_b()]), _ctx()))
    line = _purchase_lines(root)[0]
    assert line.find("sellerRegCode").text == "10555555"
    assert line.find("invoiceSumVat").text == "1364.00"
    assert line.find("vatSum").text == "264.00"
    assert line.find("vatInPeriod").text == "264.00"
    assert line.find("comments").text == "12"
    # Part B has NO rate column in the real schema.
    assert line.find("taxRate") is None


def test_kmd_inf_part_a_csv_is_symbol_rows_no_header() -> None:
    doc = build_kmd_inf_part_a_csv_document(_listing([_row_a(), _row_a()], []), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 2  # two rows, no column-name header
    cells = lines[0].split(";")
    assert cells[0] == "A"
    assert cells[1] == "10111111"     # buyerRegCode
    assert cells[6] == "24"           # taxRate classifier


def test_kmd_inf_part_a_csv_empty_listing_is_empty() -> None:
    assert build_kmd_inf_part_a_csv_document(_listing([], []), _ctx()) == b""


def test_kmd_inf_part_b_csv_values() -> None:
    doc = build_kmd_inf_part_b_csv_document(_listing([], [_row_b()]), _ctx())
    cells = doc.decode("utf-8").strip("\r\n").split("\r\n")[0].split(";")
    # B;sellerRegCode;sellerName;invoiceNumber;invoiceDate;invoiceSumVat;vatSum;vatInPeriod;comments
    assert cells[0] == "B"
    assert cells[1] == "10555555"     # sellerRegCode
    assert cells[5] == "1364.00"      # invoiceSumVat
    assert cells[6] == "264.00"       # vatSum
    assert cells[7] == "264.00"       # vatInPeriod
    assert cells[8] == "12"           # comments
