"""Local koondvaade reconciliation — pure core (no DB).

Proves the exported rows reconcile, by category, to a box-engine box vector, and
that an unclassifiable row is SURFACED (never silently dropped) so
``reconciled`` cannot be True while a transaction has vanished.
"""
from __future__ import annotations

from decimal import Decimal

from saebooks.services.lodgement.kmd_2027.reconcile import reconcile
from saebooks.services.lodgement.kmd_2027.serializer import Kmd2027Row

_D = Decimal


def _rows() -> list[Kmd2027Row]:
    # Two standard domestic supplies (2400 + 1000), one exempt (875), one
    # zero-rated IC supply (1200), an IC acquisition base (2500) + its input VAT
    # (600), and a plain domestic input VAT (240).
    return [
        Kmd2027Row(1, "M_101", _D("2400"), _D("0.24")),
        Kmd2027Row(2, "M_101", _D("1000"), _D("0.24")),
        Kmd2027Row(3, "M_301", _D("875")),
        Kmd2027Row(4, "M_201", _D("1200"), _D("0.00")),
        Kmd2027Row(5, "S_101", _D("2500"), _D("0.24")),
        Kmd2027Row(6, "O_401", _D("600"), _D("0.24")),
        Kmd2027Row(7, "O_101", _D("240"), _D("0.24")),
    ]


def _box_vector() -> dict[str, Decimal]:
    # A box vector that ties to the rows above (DOMESTIC sub-boxes carry the
    # domestic supply; box 3 the zero-rated; box 8 the exempt; box 6 the IC
    # acquisition base; box 5 the total input VAT = 600 + 240).
    return {
        "1_DOMESTIC": _D("3400"), "1-1": _D("0"), "1-2": _D("0"),
        "2_DOMESTIC": _D("0"), "2-1": _D("0"), "2-2_DOMESTIC": _D("0"),
        "3": _D("1200"),
        "8": _D("875"),
        "6": _D("2500"), "7": _D("0"),
        "9": _D("0"),
        "5": _D("840"),
    }


def test_clean_period_reconciles() -> None:
    report = reconcile(_rows(), _box_vector())
    assert report.reconciled
    assert not report.unclassified_rows
    dom = report.line_for("domestic_taxable_supply")
    assert dom is not None
    assert dom.rows_total == _D("3400")
    assert dom.boxes_total == _D("3400")
    assert dom.delta == _D("0")
    assert report.line_for("input_vat").rows_total == _D("840")


def test_box_drift_surfaces_a_delta_and_blocks_reconciled() -> None:
    boxes = _box_vector()
    boxes["5"] = _D("800")  # box engine disagrees by 40 on input VAT
    report = reconcile(_rows(), boxes)
    assert not report.reconciled
    line = report.line_for("input_vat")
    assert line.delta == _D("40")


def test_tolerance_absorbs_a_cent() -> None:
    boxes = _box_vector()
    boxes["5"] = _D("839.99")
    assert not reconcile(_rows(), boxes).reconciled
    assert reconcile(_rows(), boxes, tolerance=_D("0.01")).reconciled


def test_unclassified_row_is_surfaced_not_dropped() -> None:
    """A row on a leaf with no reconcile group (M_302 = 'not supply') must
    appear in unclassified_rows and force reconciled False even though every
    box line ties."""
    rows = [*_rows(), Kmd2027Row(8, "M_302", _D("5000"))]
    report = reconcile(rows, _box_vector())
    assert not report.reconciled
    assert len(report.unclassified_rows) == 1
    assert report.unclassified_rows[0].kmdtyyp_code == "M_302"
    assert report.unclassified_rows[0].amount == _D("5000")


def test_unknown_leaf_is_surfaced() -> None:
    rows = [*_rows(), Kmd2027Row(8, "Z_999", _D("1"))]
    report = reconcile(rows, _box_vector())
    assert not report.reconciled
    assert report.unclassified_rows[0].kmdtyyp_code == "Z_999"
    assert "unknown" in report.unclassified_rows[0].reason
