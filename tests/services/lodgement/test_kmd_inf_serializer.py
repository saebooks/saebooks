"""Unit tests for the EE KMD-INF file serializer (pure — no DB).

kmd-inf-tsd scope Packet 2 (``~/.claude/plans/kmd-inf-tsd-scope.md``
§4/§7). Mirrors ``tests/services/lodgement/test_kmd_serializer.py``'s
structural-test shape, adapted for the repeating-row Part A/Part B
listing instead of a flat 28-box vector.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.kmd_inf import (
    KMD_INF_PART_A_COLUMNS,
    KMD_INF_PART_B_COLUMNS,
    KMD_INF_TAXONOMY_NS,
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
    KmdInfReportingContext,
    build_kmd_inf_part_a_csv_document,
    build_kmd_inf_part_b_csv_document,
    build_kmd_inf_xml_document,
)
from saebooks.services.lodgement.kmd_inf.mapping import (
    KMD_INF_PART_A_FIELD_NAMES,
    KMD_INF_PART_B_FIELD_NAMES,
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


def test_kmd_inf_xml_root_carries_regcode_and_period() -> None:
    doc = build_kmd_inf_xml_document(_listing([], []), _ctx())
    root = etree.fromstring(doc)
    assert root.tag == f"{{{KMD_INF_TAXONOMY_NS}}}KmdInfDeklaratsioon"
    assert root.get("regkood") == "10123456"
    assert root.get("perioodAlgus") == "2026-02-01"
    assert root.get("perioodLopp") == "2026-02-28"


def test_kmd_inf_xml_empty_listing_emits_empty_containers() -> None:
    """Zero rows -> empty OsaA/OsaB containers, not an absent element and
    not a placeholder row (scope §4: N rows, N may be 0)."""
    doc = build_kmd_inf_xml_document(_listing([], []), _ctx())
    root = etree.fromstring(doc)
    part_a = root.find(f"{{{KMD_INF_TAXONOMY_NS}}}OsaA")
    part_b = root.find(f"{{{KMD_INF_TAXONOMY_NS}}}OsaB")
    assert part_a is not None
    assert part_b is not None
    assert len(part_a) == 0
    assert len(part_b) == 0


def test_kmd_inf_xml_part_a_row_fields() -> None:
    doc = build_kmd_inf_xml_document(_listing([_row_a()], []), _ctx())
    root = etree.fromstring(doc)
    part_a = root.find(f"{{{KMD_INF_TAXONOMY_NS}}}OsaA")
    rows = part_a.findall(f"{{{KMD_INF_TAXONOMY_NS}}}OsaAKirje")
    assert len(rows) == 1
    row_el = rows[0]
    for key in KMD_INF_PART_A_COLUMNS:
        el = row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES[key]}")
        assert el is not None, f"missing element for {key!r}"
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['taxable_value']}").text == "700.00"
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['rate']}").text == "24.00"
    # lxml normalises an empty-string-assigned element's .text to None
    # (round-tripped through fromstring) — this asserts "no erisuse-kood",
    # not the empty string literal.
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['erisuse_kood']}").text is None
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['is_credit_note']}").text == "false"


def test_kmd_inf_xml_credit_note_row_is_signed_negative() -> None:
    doc = build_kmd_inf_xml_document(_listing([_row_a(credit_note=True)], []), _ctx())
    root = etree.fromstring(doc)
    row_el = root.find(f"{{{KMD_INF_TAXONOMY_NS}}}OsaA/{{{KMD_INF_TAXONOMY_NS}}}OsaAKirje")
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['taxable_value']}").text == "-700.00"
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_A_FIELD_NAMES['is_credit_note']}").text == "true"


def test_kmd_inf_xml_part_b_row_fields() -> None:
    doc = build_kmd_inf_xml_document(_listing([], [_row_b()]), _ctx())
    root = etree.fromstring(doc)
    row_el = root.find(f"{{{KMD_INF_TAXONOMY_NS}}}OsaB/{{{KMD_INF_TAXONOMY_NS}}}OsaBKirje")
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_B_FIELD_NAMES['input_vat']}").text == "264.00"
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_B_FIELD_NAMES['document_total_incl_vat']}").text == "1364.00"
    assert row_el.find(f"{{{KMD_INF_TAXONOMY_NS}}}{KMD_INF_PART_B_FIELD_NAMES['erisuse_kood']}").text == "12"


def test_kmd_inf_part_a_csv_header_and_row_count() -> None:
    doc = build_kmd_inf_part_a_csv_document(_listing([_row_a(), _row_a()], []), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 3  # header + 2 rows
    header = lines[0].split(";")
    assert header[:3] == ["regkood", "periood_algus", "periood_lopp"]
    assert len(header) == 3 + len(KMD_INF_PART_A_COLUMNS)
    row = lines[1].split(";")
    assert row[:3] == ["10123456", "2026-02-01", "2026-02-28"]


def test_kmd_inf_part_a_csv_empty_listing_is_header_only() -> None:
    doc = build_kmd_inf_part_a_csv_document(_listing([], []), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 1


def test_kmd_inf_part_b_csv_header_and_values() -> None:
    doc = build_kmd_inf_part_b_csv_document(_listing([], [_row_b()]), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    header = lines[0].split(";")
    row = lines[1].split(";")
    values = dict(zip(header, row))
    assert values[KMD_INF_PART_B_FIELD_NAMES["input_vat"]] == "264.00"
    assert values[KMD_INF_PART_B_FIELD_NAMES["document_total_incl_vat"]] == "1364.00"
    assert len(header) == 3 + len(KMD_INF_PART_B_COLUMNS)
