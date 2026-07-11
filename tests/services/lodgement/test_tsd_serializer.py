"""Unit tests for the EE TSD file serializer (pure — no DB).

Pinned to the real e-MTA ``tsd_vorm`` format (person->payment nesting) — see
``saebooks/services/lodgement/tsd/mapping.py``.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import (
    TsdLisa1Row,
    TsdListing,
    TsdMainTotals,
    TsdReportingContext,
    build_tsd_lisa1_csv_document,
    build_tsd_main_csv_document,
    build_tsd_xml_document,
)

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )


def _row(**overrides: object) -> TsdLisa1Row:
    defaults = dict(
        employee_id=uuid.uuid4(), isikukood="38001010000",
        payment_type_code="PLACEHOLDER_PAYMENT_TYPE_WAGES",
        gross=_D("500.00"), basic_exemption_applied=_D("700.00"),
        income_tax=_D("0.00"), unemployment_employee=_D("8.00"),
        pillar_ii=_D("10.00"), social_tax=_D("292.38"),
        unemployment_employer=_D("4.00"), pay_run_id=uuid.uuid4(),
        payment_date=date(2026, 2, 25),
    )
    defaults.update(overrides)
    return TsdLisa1Row(**defaults)  # type: ignore[arg-type]


def _main(**overrides: object) -> TsdMainTotals:
    defaults = dict(
        employee_count=1, total_gross=_D("500.00"), total_income_tax=_D("0.00"),
        total_unemployment_employee=_D("8.00"), total_unemployment_employer=_D("4.00"),
        total_social_tax=_D("292.38"), total_pillar_ii=_D("10.00"),
    )
    defaults.update(overrides)
    return TsdMainTotals(**defaults)  # type: ignore[arg-type]


def _listing(main: TsdMainTotals, rows: list[TsdLisa1Row]) -> TsdListing:
    return TsdListing(
        company_id=uuid.uuid4(), period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
        main=main, lisa1=rows,
    )


def _isik_list(root: etree._Element) -> etree._Element:
    return root.find("tsd_L1_0").find("aIsikList")


def test_tsd_xml_envelope_uses_real_tsd_vorm_shape() -> None:
    root = etree.fromstring(build_tsd_xml_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("regKood").text == "10123456"
    assert root.find("c108_Aasta").text == "2026"
    assert root.find("c109_Kuu").text == "2"
    assert root.find("laadimisViis").text == "L"
    assert root.find("vorm").text == "TSD"


def test_tsd_xml_main_rollup_merges_unemployment_premium() -> None:
    root = etree.fromstring(build_tsd_xml_document(_listing(_main(), [_row()]), _ctx()))
    assert root.find("c110_Tm").text == "0.00"       # income tax
    assert root.find("c115_Sm").text == "292.38"     # social tax
    assert root.find("c116_Tk").text == "12.00"      # unemployment employee(8) + employer(4)
    assert root.find("c117_Kp").text == "10.00"      # funded pension


def test_tsd_xml_empty_lisa1_emits_empty_aisiklist() -> None:
    root = etree.fromstring(build_tsd_xml_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx()))
    assert root.find("tsd_L1_0") is not None
    assert len(_isik_list(root)) == 0


def test_tsd_xml_person_payment_nesting_and_payment_type_code() -> None:
    root = etree.fromstring(build_tsd_xml_document(_listing(_main(), [_row()]), _ctx()))
    persons = _isik_list(root).findall("tsd_L1_A_Isik")
    assert len(persons) == 1
    person = persons[0]
    assert person.find("c1000_Kood").text == "38001010000"
    payments = person.find("vmList").findall("tsd_L1_A_Vm")
    assert len(payments) == 1
    vm = payments[0]
    assert vm.find("c1020_ValiKood").text == "10"      # mapped from PLACEHOLDER_PAYMENT_TYPE_WAGES
    assert vm.find("c1030_Summa").text == "500.00"
    assert vm.find("c1100_Sm").text == "292.38"
    assert vm.find("c1170_Tm").text == "0.00"
    # basic_exemption_applied / payment_date / employee_id are NOT wire fields.
    assert vm.find("payment_date") is None
    assert vm.find("employee_id") is None


def test_tsd_xml_multiple_payments_per_person_nest_under_one_isik() -> None:
    root = etree.fromstring(
        build_tsd_xml_document(_listing(_main(), [_row(gross=_D("500.00")), _row(gross=_D("300.00"))]), _ctx())
    )
    persons = _isik_list(root).findall("tsd_L1_A_Isik")
    assert len(persons) == 1  # same isikukood -> one Isik
    assert len(persons[0].find("vmList").findall("tsd_L1_A_Vm")) == 2


def test_tsd_lisa1_csv_codes_header_comma_decimals_and_bom() -> None:
    doc = build_tsd_lisa1_csv_document(_listing(_main(), [_row()]), _ctx())
    assert doc.startswith("﻿".encode("utf-8"))  # UTF-8 BOM
    lines = doc.decode("utf-8-sig").strip("\r\n").split("\r\n")
    assert len(lines) == 2  # code header + 1 row
    assert lines[0].split(";") == ["1000", "1020", "1030", "1100", "1110", "1130", "1140", "1170"]
    cells = lines[1].split(";")
    assert cells[0] == '"38001010000"'   # text field quoted
    assert cells[1] == '"10"'            # payment type quoted
    assert cells[2] == "500,00"          # comma decimal
    assert cells[3] == "292,38"


def test_tsd_lisa1_csv_empty_listing_is_header_only() -> None:
    doc = build_tsd_lisa1_csv_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx())
    lines = doc.decode("utf-8-sig").strip("\r\n").split("\r\n")
    assert len(lines) == 1


def test_tsd_main_csv_codes_header_and_values() -> None:
    doc = build_tsd_main_csv_document(_listing(_main(), [_row()]), _ctx())
    lines = doc.decode("utf-8-sig").strip("\r\n").split("\r\n")
    assert lines[0].split(";") == ["108", "109", "110", "115", "116", "117"]
    cells = lines[1].split(";")
    assert cells[0] == "2026"    # year
    assert cells[1] == "2"       # month
    assert cells[4] == "12,00"   # merged unemployment premium, comma decimal
