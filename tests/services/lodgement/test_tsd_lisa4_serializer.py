"""Unit tests for the EE TSD Lisa 4 (fringe benefits / erisoodustused)
serializer (pure — no DB).

Semi-strong golden — ``tsd_L4_0`` header totals ARE populated in the
official example (build-plan §0.3); no repeating rows exist in the XSD
for this annex at all, so a header-only model is the complete shape.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import TsdLisa4Header, TsdReportingContext
from saebooks.services.lodgement.tsd.serializer import build_tsd_lisa4_xml_document

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def _header() -> TsdLisa4Header:
    # Values lifted from the official populated example.
    return TsdLisa4Header(
        electricity_expense=_D("500"), fuel_expense=_D("100"), housing_benefit=_D("1000"),
        transport_benefit=_D("300"), other_benefit=_D("70.5"), total_expenses_incl_vat=_D("3111.85"),
        social_tax=_D("1016.17"),
    )


def test_lisa4_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa4_xml_document(_header(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L4_0") is not None


def test_lisa4_header_fields_match_official_example_values() -> None:
    root = etree.fromstring(build_tsd_lisa4_xml_document(_header(), _ctx()))
    lisa4 = root.find("tsd_L4_0")
    assert lisa4.find("c4000_ElKulu").text == "500.00"
    assert lisa4.find("c4010_KiKulu").text == "100.00"
    assert lisa4.find("c4030_Is").text == "1000.00"
    assert lisa4.find("c4140_EsSumma").text == "3111.85"
    assert lisa4.find("c4180_Sm").text == "1016.17"


def test_lisa4_empty_header_emits_empty_block() -> None:
    root = etree.fromstring(build_tsd_lisa4_xml_document(TsdLisa4Header(), _ctx()))
    assert len(root.find("tsd_L4_0")) == 0
