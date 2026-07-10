"""Unit tests for the EE KMD file serializer (pure — no DB).

KMD-formula support Packet 4 (see
``~/.claude/plans/kmd-formula-support-scope.md`` §5/§6/§7 Packet 4).
Mirrors ``tests/services/lodgement/test_sbr_bas.py``'s structural-test
shape for the AU SBR generator.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.kmd import (
    KMD_BOX_ORDER,
    KMD_TAXONOMY_NS,
    KmdFigures,
    KmdReportingContext,
    build_kmd_csv_document,
    build_kmd_xml_document,
)
from saebooks.services.lodgement.kmd.mapping import KMD_FIELD_NAMES


def _ctx() -> KmdReportingContext:
    return KmdReportingContext(
        regcode="12345678", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )


def _figures() -> KmdFigures:
    return KmdFigures.from_box_amounts(
        {"1": Decimal("10000.00"), "2": Decimal("2000.00"), "4": Decimal("2710.00")}
    )


def test_kmd_box_order_has_all_28_official_boxes() -> None:
    assert len(KMD_BOX_ORDER) == 28
    assert KMD_BOX_ORDER[0] == "1"
    assert KMD_BOX_ORDER[-1] == "13"
    # internal helper boxes from the EE seed (Packet 3) must NOT appear.
    assert "1_DOMESTIC" not in KMD_BOX_ORDER
    assert "1_RC" not in KMD_BOX_ORDER
    assert "5_DOMESTIC" not in KMD_BOX_ORDER
    assert "5_RC" not in KMD_BOX_ORDER


def test_kmd_xml_is_wellformed_with_regcode_and_period() -> None:
    doc = build_kmd_xml_document(_figures(), _ctx())
    root = etree.fromstring(doc)
    assert root.tag == f"{{{KMD_TAXONOMY_NS}}}KmdDeklaratsioon"
    assert root.get("regkood") == "12345678"
    assert root.get("perioodAlgus") == "2026-01-01"
    assert root.get("perioodLopp") == "2026-01-31"


def test_kmd_xml_emits_all_28_boxes_including_nils() -> None:
    doc = build_kmd_xml_document(_figures(), _ctx())
    root = etree.fromstring(doc)
    for box_code in KMD_BOX_ORDER:
        el = root.find(f"{{{KMD_TAXONOMY_NS}}}{KMD_FIELD_NAMES[box_code]}")
        assert el is not None, f"missing element for box {box_code!r}"
    box_3 = root.find(f"{{{KMD_TAXONOMY_NS}}}{KMD_FIELD_NAMES['3']}")
    assert box_3.text == "0.00"  # reported nil, not absent


def test_kmd_xml_values_are_two_decimal_places() -> None:
    doc = build_kmd_xml_document(_figures(), _ctx())
    root = etree.fromstring(doc)
    box_1 = root.find(f"{{{KMD_TAXONOMY_NS}}}{KMD_FIELD_NAMES['1']}")
    assert box_1.text == "10000.00"
    box_4 = root.find(f"{{{KMD_TAXONOMY_NS}}}{KMD_FIELD_NAMES['4']}")
    assert box_4.text == "2710.00"


def test_kmd_csv_has_header_and_one_data_row() -> None:
    doc = build_kmd_csv_document(_figures(), _ctx())
    text = doc.decode("utf-8")
    lines = text.strip("\r\n").split("\r\n")
    assert len(lines) == 2
    header = lines[0].split(";")
    row = lines[1].split(";")
    assert header[:3] == ["regkood", "periood_algus", "periood_lopp"]
    assert row[:3] == ["12345678", "2026-01-01", "2026-01-31"]
    assert len(header) == len(row) == 3 + len(KMD_BOX_ORDER)


def test_kmd_csv_box_1_and_4_values() -> None:
    doc = build_kmd_csv_document(_figures(), _ctx())
    lines = doc.decode("utf-8").strip("\r\n").split("\r\n")
    header = lines[0].split(";")
    row = lines[1].split(";")
    values = dict(zip(header, row))
    assert values[KMD_FIELD_NAMES["1"]] == "10000.00"
    assert values[KMD_FIELD_NAMES["4"]] == "2710.00"
    assert values[KMD_FIELD_NAMES["3"]] == "0.00"


def test_kmd_figures_from_figures_json_exact_key_match_no_collision() -> None:
    """Regression guard: unlike sbr.bas.BasFigures.from_figures_json's
    separator-stripping lookup (which would fold "1-1" and "1-2" toward
    colliding keys), KmdFigures.from_figures_json must match box codes
    EXACTLY — "1-1" and "1-2" must resolve independently."""
    figs = KmdFigures.from_figures_json(
        {
            "1-1": {"amount": "111.11"},
            "1-2": {"amount": "222.22"},
            "1": "333.33",
        }
    )
    assert figs.amount("1-1") == Decimal("111.11")
    assert figs.amount("1-2") == Decimal("222.22")
    assert figs.amount("1") == Decimal("333.33")


def test_kmd_figures_from_box_amounts_ignores_internal_helper_boxes() -> None:
    figs = KmdFigures.from_box_amounts(
        {"1": Decimal("4000.00"), "1_DOMESTIC": Decimal("0.00"), "1_RC": Decimal("4000.00")}
    )
    assert figs.amount("1") == Decimal("4000.00")
    assert figs.boxes.get("1_DOMESTIC") is None
