"""Unit tests for the EE TSD Lisa 5 (gifts / donations / entertainment)
serializer (pure — no DB).

Semi-strong golden — ``tsd_L5_0`` header totals ARE populated in the
official example (build-plan §0.3); the ``tsd_L5_3`` repeating list is
NOT populated there and is not modelled here (see generator.py's Lisa 5
section docstring).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import TsdLisa5Header, TsdReportingContext
from saebooks.services.lodgement.tsd.serializer import build_tsd_lisa5_xml_document

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def _header() -> TsdLisa5Header:
    # Values lifted from the official populated example.
    return TsdLisa5Header(
        gifts_total=_D("500.5"), prior_gift_month=_D("500"), prior_gift_year=_D("500"),
        gift_income_tax=_D("139.62"), special_income_tax_payable=_D("280.79"),
    )


def test_lisa5_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa5_xml_document(_header(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L5_0") is not None


def test_lisa5_header_fields_match_official_example_values() -> None:
    root = etree.fromstring(build_tsd_lisa5_xml_document(_header(), _ctx()))
    lisa5 = root.find("tsd_L5_0")
    assert lisa5.find("c5000_Ki").text == "500.50"
    # c5010_IKiKuu / c5020_IKiAasta are xs:decimal in the XSD (NOT xs:long,
    # unlike every other month/year pair) — 2dp formatting is correct here.
    assert lisa5.find("c5010_IKiKuu").text == "500.00"
    assert lisa5.find("c5070_ITm").text == "139.62"
    assert lisa5.find("c5160_TasTmEj").text == "280.79"


def test_lisa5_empty_header_emits_empty_block() -> None:
    root = etree.fromstring(build_tsd_lisa5_xml_document(TsdLisa5Header(), _ctx()))
    assert len(root.find("tsd_L5_0")) == 0
