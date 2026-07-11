"""Unit tests for the EE KMD file serializer (pure — no DB).

Pinned to the real e-MTA ``vatDeclaration`` (KMD6) format — see
``saebooks/services/lodgement/kmd/mapping.py``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.kmd import (
    KMD_EMITTED_BOX_ORDER,
    KmdFigures,
    KmdReportingContext,
    build_kmd_csv_document,
    build_kmd_xml_document,
)
from saebooks.services.lodgement.kmd.mapping import KMD_CSV_BODY_COLUMNS, KMD_FIELD_NAMES


def _ctx() -> KmdReportingContext:
    return KmdReportingContext(
        regcode="12345678", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )


def _figures() -> KmdFigures:
    return KmdFigures.from_box_amounts(
        {"1": Decimal("10000.00"), "2": Decimal("2000.00"), "5": Decimal("840.00")}
    )


def test_kmd_emitted_box_order_is_24_filable_boxes() -> None:
    assert len(KMD_EMITTED_BOX_ORDER) == 24
    assert KMD_EMITTED_BOX_ORDER[0] == "1"
    assert KMD_EMITTED_BOX_ORDER[-1] == "11"
    # e-MTA-computed boxes are not submitted.
    for computed in ("4", "4-1", "12", "13"):
        assert computed not in KMD_EMITTED_BOX_ORDER
    # internal seed helper boxes must never appear.
    for helper in ("1_DOMESTIC", "1_RC", "5_DOMESTIC", "5_RC"):
        assert helper not in KMD_EMITTED_BOX_ORDER


def test_kmd_xml_envelope_uses_real_vatdeclaration_shape() -> None:
    root = etree.fromstring(build_kmd_xml_document(_figures(), _ctx()))
    assert root.tag == "vatDeclaration"
    assert root.find("taxPayerRegCode").text == "12345678"
    assert root.find("year").text == "2026"
    assert root.find("month").text == "01"
    assert root.find("declarationType").text == "1"
    assert root.find("version").text == "KMD6"  # 2026-01 is 07.2025+
    body = root.find("declarationBody")
    assert body is not None
    # four mandatory flags lead the body.
    assert [e.tag for e in body[:4]] == [
        "noSales", "noPurchases", "sumPerPartnerSales", "sumPerPartnerPurchases",
    ]


def test_kmd_xml_emits_all_24_boxes_including_nils() -> None:
    body = etree.fromstring(build_kmd_xml_document(_figures(), _ctx())).find("declarationBody")
    for box_code in KMD_EMITTED_BOX_ORDER:
        assert body.find(KMD_FIELD_NAMES[box_code]) is not None, f"missing box {box_code!r}"
    # box 3 (transactionsZeroVat) reported nil, not absent.
    assert body.find(KMD_FIELD_NAMES["3"]).text == "0.00"
    # 24% standard rate maps to transactions24 (KMD6).
    assert KMD_FIELD_NAMES["1"] == "transactions24"


def test_kmd_xml_values_are_two_decimal_places() -> None:
    body = etree.fromstring(build_kmd_xml_document(_figures(), _ctx())).find("declarationBody")
    assert body.find("transactions24").text == "10000.00"
    assert body.find("inputVatTotal").text == "840.00"


def test_kmd_csv_is_a_single_symbol_row() -> None:
    lines = build_kmd_csv_document(_figures(), _ctx()).decode("utf-8").strip("\r\n").split("\r\n")
    assert len(lines) == 1  # no column-name header row
    cells = lines[0].split(";")
    assert cells[0] == "KMD6"
    assert cells[1:5] == ["false", "false", "false", "false"]  # the four flags
    # positional body = 24 boxes + the 2 empty car-count columns.
    assert len(cells) == 5 + len(KMD_CSV_BODY_COLUMNS) == 5 + 26
    # first box after the flags is transactions24 (24%).
    assert cells[5] == "10000.00"
    # the two car-count columns sit at their documented slots, emitted empty.
    empty_idx = [5 + i for i, (kind, _) in enumerate(KMD_CSV_BODY_COLUMNS) if kind == "empty"]
    assert [cells[i] for i in empty_idx] == ["", ""]


def test_kmd_figures_from_figures_json_exact_key_match_no_collision() -> None:
    figs = KmdFigures.from_figures_json(
        {"1-1": {"amount": "111.11"}, "1-2": {"amount": "222.22"}, "1": "333.33"}
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
