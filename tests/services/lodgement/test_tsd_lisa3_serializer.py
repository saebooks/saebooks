"""Unit tests for the EE TSD Lisa 3 (special: PE/CFC/disguised
distribution) serializer (pure — no DB).

⚠ WEAKEST-GOLDEN annex — ``tsd_L3_0`` is ABSENT from the official
populated example entirely (build-plan §0.3); these are hand-authored
values, XSD-validated only (see ``test_emta_schema_validation.py``), not
cross-checked against any real filed data.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import TsdLisa3Header, TsdReportingContext
from saebooks.services.lodgement.tsd.serializer import build_tsd_lisa3_xml_document

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def test_lisa3_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa3_xml_document(TsdLisa3Header(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L3_0") is not None


def test_lisa3_header_fields_emitted_when_present() -> None:
    header = TsdLisa3Header(
        profit_removed_from_pe=_D("1000.00"), profit_treaty_exempt=_D("300.00"),
        treaty_country_code="FI", cfc_profit=_D("400.00"),
    )
    root = etree.fromstring(build_tsd_lisa3_xml_document(header, _ctx()))
    lisa3 = root.find("tsd_L3_0")
    assert lisa3.find("c3000_VKasum").text == "1000.00"
    assert lisa3.find("c3010_Mv").text == "300.00"
    assert lisa3.find("c3020_RiikKood").text == "FI"
    assert lisa3.find("c3815_AyTulu").text == "400.00"


def test_lisa3_empty_header_emits_empty_block() -> None:
    root = etree.fromstring(build_tsd_lisa3_xml_document(TsdLisa3Header(), _ctx()))
    lisa3 = root.find("tsd_L3_0")
    assert len(lisa3) == 0
