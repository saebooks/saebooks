"""Unit tests for the EE TSD Lisa 2 (non-resident payments/withholding)
serializer (pure — no DB).

Pinned to the real e-MTA ``tsd_L2_0`` shape (person->payment->income-type
nesting, A/B/investment-fund subforms) — see
``saebooks/services/lodgement/tsd/mapping.py``'s Module 1 section. Sample
values below are lifted from the official populated example
(``tsd_naide_xml_01.01.2025_eng.xml``) so this file doubles as a
correctness cross-check, not just a structural smoke test.
"""
from __future__ import annotations

from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import (
    TsdLisa2ARow,
    TsdLisa2BRow,
    TsdLisa2InvFondRow,
    TsdLisa2Listing,
    TsdLisa2MvtRow,
    TsdReportingContext,
    build_tsd_lisa2_a_csv_document,
    build_tsd_lisa2_xml_document,
    compute_lisa2_totals,
)

_D = Decimal


def _ctx() -> TsdReportingContext:
    from datetime import date

    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def _a_row(**overrides: object) -> TsdLisa2ARow:
    defaults = dict(
        isikukood="45212181423", name="OIE-MARET SOUDNITSYNA", country_code="FI",
        payment_type_code="120", gross=_D("1111"), a1_certificate_country_code=None,
        social_tax_base=_D("1111"), incapacity_pension_deducted=None,
        prior_month_rate_deducted=None, minimum_social_tax_increase=None,
        social_tax=_D("366.63"), unemployment_base=_D("1111"),
        unemployment_employee=None, unemployment_employer=_D("8.89"),
        income_tax_base=_D("1111"), income_tax_rate=_D("22"), income_tax=_D("89.54"),
        mvt=(TsdLisa2MvtRow(source_code="650", amount=_D("704")),),
    )
    defaults.update(overrides)
    return TsdLisa2ARow(**defaults)  # type: ignore[arg-type]


def _b_row(**overrides: object) -> TsdLisa2BRow:
    defaults = dict(
        isikukood="34501175307", name="PEEBO GERKO", payment_type_code="120",
        gross=_D("5000"), year=2023, month=1, reason_code="VR",
        social_tax_base=_D("5000"), social_tax_base_deducted=None,
        social_tax_base_increase=None, social_tax_base_adjustment=None,
        social_tax=_D("1650"), unemployment_base=_D("5000"),
        unemployment_employee=None, unemployment_employer=_D("40"),
        income_tax_base=_D("5000"), income_tax_rate=_D("20"), income_tax=_D("959.2"),
    )
    defaults.update(overrides)
    return TsdLisa2BRow(**defaults)  # type: ignore[arg-type]


def _inv_row(**overrides: object) -> TsdLisa2InvFondRow:
    defaults = dict(
        fund_code="60004693", fund_name="LEPINGULISE INVESTEERIMISFONDI NÄIDE",
        fund_country_code=None, manager_code="71016977", manager_name="HARJU MAKSUAMET UUS",
        manager_country_code=None, participation_percent=_D("15"),
        payment_type_code="198", amount=_D("20300"), income_tax=_D("4466"),
    )
    defaults.update(overrides)
    return TsdLisa2InvFondRow(**defaults)  # type: ignore[arg-type]


def _lisa2(**kwargs: object) -> TsdLisa2Listing:
    defaults: dict = dict(a_rows=[_a_row()], b_rows=[_b_row()], inv_fond_rows=[_inv_row()])
    defaults.update(kwargs)
    if "totals" not in defaults:
        defaults["totals"] = compute_lisa2_totals(
            defaults["a_rows"], defaults["b_rows"], defaults["inv_fond_rows"]
        )
    return TsdLisa2Listing(**defaults)  # type: ignore[arg-type]


def test_lisa2_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L2_0") is not None


def test_lisa2_a_person_payment_nesting() -> None:
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(), _ctx()))
    a_list = root.find("tsd_L2_0").find("aIsikList")
    persons = a_list.findall("tsd_L2_A_Isik")
    assert len(persons) == 1
    person = persons[0]
    assert person.find("c2000_Kood").text == "45212181423"
    assert person.find("c2010_Nimi").text == "OIE-MARET SOUDNITSYNA"
    payments = person.find("vmList").findall("tsd_L2_A_Vm")
    assert len(payments) == 1
    vm = payments[0]
    assert vm.find("c2020_RiikKood").text == "FI"
    assert vm.find("c2030_ValiKood").text == "120"
    assert vm.find("c2040_Summa").text == "1111.00"
    assert vm.find("c2110_Sm").text == "366.63"
    # optional-with-None fields are OMITTED, not emitted empty.
    assert vm.find("c2060_RiikKood") is None
    assert vm.find("c2130_Tk") is None


def test_lisa2_a_mvt_nested_under_vm() -> None:
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(), _ctx()))
    vm = root.find(".//tsd_L2_A_Vm")
    mvt_list = vm.find("mvtList")
    assert mvt_list is not None
    mvts = mvt_list.findall("tsd_L2_A_Mvt")
    assert len(mvts) == 1
    assert mvts[0].find("c2154_TuliKood").text == "650"
    assert mvts[0].find("c2155_Summa").text == "704.00"


def test_lisa2_multiple_a_payments_same_person_nest_under_one_isik() -> None:
    rows = [_a_row(gross=_D("1111")), _a_row(gross=_D("600"), payment_type_code="123")]
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(a_rows=rows), _ctx()))
    persons = root.find("tsd_L2_0").find("aIsikList").findall("tsd_L2_A_Isik")
    assert len(persons) == 1
    assert len(persons[0].find("vmList").findall("tsd_L2_A_Vm")) == 2


def test_lisa2_b_person_payment_nesting_and_reason_explanation() -> None:
    row = _b_row(reason_code="MUU", reason_explanation="Eksitus väljamaksel")
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(b_rows=[row]), _ctx()))
    person = root.find("tsd_L2_0").find("bIsikList").find("tsd_L2_B_Isik")
    assert person.find("c2300_Kood").text == "34501175307"
    vm = person.find("vmList").find("tsd_L2_B_Vm")
    assert vm.find("c2320_ValiKood").text == "120"
    assert vm.find("c2340_Aasta").text == "2023"     # xs:long -> plain int, no "2023.00"
    assert vm.find("c2350_Kuu").text == "1"
    assert vm.find("pohjusSelgitus").text == "Eksitus väljamaksel"


def test_lisa2_invfond_grouping_and_payments() -> None:
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(), _ctx()))
    fond_list = root.find("tsd_L2_0").find("invFondList")
    funds = fond_list.findall("tsd_L2_2_Inv_Fond")
    assert len(funds) == 1
    fund = funds[0]
    assert fund.find("c2700_Kood").text == "60004693"
    assert fund.find("c2780_Osalus").text == "15.00"
    payments = fund.find("vmList").findall("tsd_L2_2_Vm")
    assert len(payments) == 1
    assert payments[0].find("c2760_ValiKood").text == "198"
    assert payments[0].find("c2790_Tm").text == "4466.00"


def test_lisa2_totals_are_literal_sums_of_rows() -> None:
    root = etree.fromstring(build_tsd_lisa2_xml_document(_lisa2(), _ctx()))
    lisa2 = root.find("tsd_L2_0")
    assert lisa2.find("c2200_Smvm").text == "1111.00"
    assert lisa2.find("c2210_Sm").text == "366.63"
    assert lisa2.find("c2500_Smvm").text == "5000.00"
    assert lisa2.find("c2800_InvTm").text == "4466.00"


def test_lisa2_empty_listing_omits_all_optional_lists() -> None:
    root = etree.fromstring(
        build_tsd_lisa2_xml_document(TsdLisa2Listing(), _ctx())
    )
    lisa2 = root.find("tsd_L2_0")
    assert lisa2 is not None
    assert len(lisa2) == 0   # minOccurs=0 everywhere -> valid empty block


def test_lisa2_a_csv_codes_header_and_text_quoting() -> None:
    doc = build_tsd_lisa2_a_csv_document([_a_row()])
    assert doc.startswith("﻿".encode())
    lines = doc.decode("utf-8-sig").strip("\r\n").split("\r\n")
    assert len(lines) == 2
    assert lines[0].split(";")[0:5] == ["2000", "2010", "2020", "2030", "2040"]
    cells = lines[1].split(";")
    assert cells[0] == '"45212181423"'
    assert cells[4] == "1111,00"    # comma decimal


def test_lisa2_a_csv_empty_is_header_only() -> None:
    doc = build_tsd_lisa2_a_csv_document([])
    lines = doc.decode("utf-8-sig").strip("\r\n").split("\r\n")
    assert len(lines) == 1
