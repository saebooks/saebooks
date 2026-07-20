"""Unit tests for the EE TSD Lisa 6 (non-business expenses) serializer
(pure — no DB).

Strong golden — ``tsd_L6_0`` header totals + ``tsd_L6_1``/``tsd_L6_2`` ARE
populated in the official example (build-plan §0.3); ``tsd_L6_3`` is not
populated there but is modelled anyway (trivial 2-field XSD shape).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import (
    TsdLisa6Header,
    TsdLisa6Listing,
    TsdLisa6Row1,
    TsdLisa6Row2,
    TsdLisa6Row3,
    TsdReportingContext,
)
from saebooks.services.lodgement.tsd.serializer import build_tsd_lisa6_xml_document

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def _listing() -> TsdLisa6Listing:
    # Values lifted from the official populated example.
    return TsdLisa6Listing(
        header=TsdLisa6Header(
            related_party_value_diff=_D("45000"), fines_penalties=_D("4800"),
            total_taxable_amount=_D("17469"), income_tax_payable=_D("6353.79"),
        ),
        rows1=[
            TsdLisa6Row1(month=5, year=2023, amount=_D("44000")),
            TsdLisa6Row1(month=2, year=2024, amount=_D("511")),
        ],
        rows2=[
            TsdLisa6Row2(
                related_party_code="556016-0680", related_party_name="Ericsson AB",
                country_code="SE", taxable_amount=_D("40000"), payment_type_code="621",
            ),
        ],
    )


def test_lisa6_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa6_xml_document(_listing(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L6_0") is not None


def test_lisa6_header_totals() -> None:
    root = etree.fromstring(build_tsd_lisa6_xml_document(_listing(), _ctx()))
    lisa6 = root.find("tsd_L6_0")
    assert lisa6.find("c6000_TVahe").text == "45000.00"
    assert lisa6.find("c6150_SumKokku").text == "17469.00"
    assert lisa6.find("c6160_Tasutav").text == "6353.79"


def test_lisa6_row1_tax_base_reduction_rows_int_month_year() -> None:
    root = etree.fromstring(build_tsd_lisa6_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L6_0").find("tsd_L6_1List").findall("tsd_L6_1")
    assert len(rows) == 2
    assert rows[0].find("c6141_Kuu").text == "5"      # int, not "5.00"
    assert rows[0].find("c6142_Aasta").text == "2023"
    assert rows[0].find("c6143_Summa").text == "44000.00"


def test_lisa6_row2_related_party_row() -> None:
    root = etree.fromstring(build_tsd_lisa6_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L6_0").find("tsd_L6_2List").findall("tsd_L6_2")
    assert len(rows) == 1
    assert rows[0].find("c6200_Kood").text == "556016-0680"
    assert rows[0].find("c6230_MSumma").text == "40000.00"
    assert rows[0].find("c6240_ValiKood").text == "621"


def test_lisa6_row3_optional_payment_type_omitted_when_none() -> None:
    listing = TsdLisa6Listing(
        rows2=[TsdLisa6Row2(related_party_code="X", related_party_name="Y",
                             country_code="EE", taxable_amount=_D("10"))],
        rows3=[TsdLisa6Row3(year=2024, amount=_D("50"))],
    )
    root = etree.fromstring(build_tsd_lisa6_xml_document(listing, _ctx()))
    row2 = root.find(".//tsd_L6_2")
    assert row2.find("c6240_ValiKood") is None    # omitted, not empty
    row3 = root.find(".//tsd_L6_3")
    assert row3.find("c6300_Aasta").text == "2024"


def test_lisa6_empty_listing_omits_all_optional_lists() -> None:
    root = etree.fromstring(build_tsd_lisa6_xml_document(TsdLisa6Listing(), _ctx()))
    assert len(root.find("tsd_L6_0")) == 0
