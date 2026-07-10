"""Unit tests for the EE TSD file serializer (pure — no DB).

kmd-inf-tsd scope Packet 5 (``~/.claude/plans/kmd-inf-tsd-scope.md``
§4/§7). Mirrors ``tests/services/lodgement/test_kmd_inf_serializer.py``'s
structural-test shape, adapted for TSD's MAIN-aggregate-block +
Lisa-1-repeating-listing combination instead of two homogeneous parts.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import (
    TSD_LISA1_COLUMNS,
    TSD_MAIN_COLUMNS,
    TSD_TAXONOMY_NS,
    TsdLisa1Row,
    TsdListing,
    TsdMainTotals,
    TsdReportingContext,
    build_tsd_lisa1_csv_document,
    build_tsd_main_csv_document,
    build_tsd_xml_document,
)
from saebooks.services.lodgement.tsd.mapping import (
    TSD_LISA1_FIELD_NAMES,
    TSD_MAIN_FIELD_NAMES,
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


def test_tsd_xml_root_carries_regcode_and_period() -> None:
    doc = build_tsd_xml_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx())
    root = etree.fromstring(doc)
    assert root.tag == f"{{{TSD_TAXONOMY_NS}}}TsdDeklaratsioon"
    assert root.get("regkood") == "10123456"
    assert root.get("perioodAlgus") == "2026-02-01"
    assert root.get("perioodLopp") == "2026-02-28"


def test_tsd_xml_main_emits_all_seven_fields_even_when_zero() -> None:
    zero_main = TsdMainTotals(
        employee_count=0, total_gross=_D("0"), total_income_tax=_D("0"),
        total_unemployment_employee=_D("0"), total_unemployment_employer=_D("0"),
        total_social_tax=_D("0"), total_pillar_ii=_D("0"),
    )
    doc = build_tsd_xml_document(_listing(zero_main, []), _ctx())
    root = etree.fromstring(doc)
    main_el = root.find(f"{{{TSD_TAXONOMY_NS}}}Pealdeklaratsioon")
    assert main_el is not None
    for key in TSD_MAIN_COLUMNS:
        el = main_el.find(f"{{{TSD_TAXONOMY_NS}}}{TSD_MAIN_FIELD_NAMES[key]}")
        assert el is not None, f"missing element for {key!r}"
    assert main_el.find(f"{{{TSD_TAXONOMY_NS}}}{TSD_MAIN_FIELD_NAMES['total_gross']}").text == "0.00"


def test_tsd_xml_empty_lisa1_emits_empty_container() -> None:
    """Zero rows -> empty Lisa1 container, not an absent element and not
    a placeholder row (mirrors kmd_inf's "N rows, N may be 0")."""
    doc = build_tsd_xml_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx())
    root = etree.fromstring(doc)
    lisa1 = root.find(f"{{{TSD_TAXONOMY_NS}}}Lisa1")
    assert lisa1 is not None
    assert len(lisa1) == 0


def test_tsd_xml_lisa1_row_fields() -> None:
    doc = build_tsd_xml_document(_listing(_main(), [_row()]), _ctx())
    root = etree.fromstring(doc)
    lisa1 = root.find(f"{{{TSD_TAXONOMY_NS}}}Lisa1")
    rows = lisa1.findall(f"{{{TSD_TAXONOMY_NS}}}Lisa1Kirje")
    assert len(rows) == 1
    row_el = rows[0]
    for key in TSD_LISA1_COLUMNS:
        el = row_el.find(f"{{{TSD_TAXONOMY_NS}}}{TSD_LISA1_FIELD_NAMES[key]}")
        assert el is not None, f"missing element for {key!r}"
    assert row_el.find(f"{{{TSD_TAXONOMY_NS}}}{TSD_LISA1_FIELD_NAMES['social_tax']}").text == "292.38"
    assert row_el.find(f"{{{TSD_TAXONOMY_NS}}}{TSD_LISA1_FIELD_NAMES['isikukood']}").text == "38001010000"
    # employee_id / pay_run_id are NOT wire fields (mapping.py docstring) —
    # neither their raw UUID nor any element named after them should
    # appear anywhere under the row.
    assert row_el.find(f"{{{TSD_TAXONOMY_NS}}}employee_id") is None
    assert row_el.find(f"{{{TSD_TAXONOMY_NS}}}pay_run_id") is None


def test_tsd_main_csv_header_and_row_count() -> None:
    doc = build_tsd_main_csv_document(_listing(_main(), [_row()]), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 2  # header + 1 aggregate row
    header = lines[0].split(";")
    assert header[:3] == ["regkood", "periood_algus", "periood_lopp"]
    assert len(header) == 3 + len(TSD_MAIN_COLUMNS)
    row = lines[1].split(";")
    assert row[:3] == ["10123456", "2026-02-01", "2026-02-28"]


def test_tsd_lisa1_csv_header_and_values() -> None:
    doc = build_tsd_lisa1_csv_document(_listing(_main(), [_row(), _row()]), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 3  # header + 2 rows
    header = lines[0].split(";")
    assert len(header) == 3 + len(TSD_LISA1_COLUMNS)
    row = lines[1].split(";")
    values = dict(zip(header, row))
    assert values[TSD_LISA1_FIELD_NAMES["social_tax"]] == "292.38"
    assert values[TSD_LISA1_FIELD_NAMES["isikukood"]] == "38001010000"


def test_tsd_lisa1_csv_empty_listing_is_header_only() -> None:
    doc = build_tsd_lisa1_csv_document(_listing(_main(employee_count=0, total_gross=_D("0")), []), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 1
