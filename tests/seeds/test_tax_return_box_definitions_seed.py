"""M1.5 · T8 — AU BAS box-definition seed: integrity + reference-DB load.

Two tests, mirroring tests/seeds/test_entity_structure_seed.py's shape:

  * ``test_au_tax_return_box_definitions_seed_valid`` — pure-unit check
    (no DB) that every row is well-formed and its ``aggregation`` string
    parses cleanly via the real
    ``tax_return_generator._parse_box_definition`` — catching a grammar
    typo in the YAML before it ever reaches a database.
  * ``test_load_tax_return_box_definitions`` — reference-DB integration
    (skipped unless ``REFERENCE_MIGRATION_DATABASE_URL`` is configured,
    same gate as ``tests/seeds/test_jurisdiction_loader.py``) proving the
    seed loads and the AU BAS box set round-trips.

KMD-formula support Packet 2 (see
~/.claude/plans/kmd-formula-support-scope.md §2/§6/§7) adds an EE
counterpart section below — pure-unit seed-integrity checks over the real
EE ``tax_return_box_definitions.yaml`` (no DB): every ``formula`` string
parses via the real parser, every box a formula references exists in the
EE KMD box set (no dangling ref), and the KMD box set is acyclic (the
formula pass's own topological sort would otherwise raise at return-
generation time, not at seed-review time).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _formula_refs,
    _FormulaParser,
    _parse_box_definition,
    _topological_order,
)

_AU_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "tax_return_box_definitions.yaml"
)

_EE_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "EE"
    / "tax_return_box_definitions.yaml"
)

_EXPECTED_BOX_CODES = {"G1", "G2", "G3", "G10", "G11", "1A", "1B"}


def test_au_tax_return_box_definitions_seed_valid() -> None:
    doc = yaml.safe_load(_AU_SEED.read_text())
    assert doc["table"] == "tax_return_box_definitions"
    assert doc["key"] == ["jurisdiction", "return_type", "box_code"]
    rows = doc["rows"]
    assert rows, "AU tax_return_box_definitions seed is empty"

    keys = [(r["jurisdiction"], r["return_type"], r["box_code"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, return_type, box_code) in AU seed"

    box_codes = {r["box_code"] for r in rows}
    assert box_codes == _EXPECTED_BOX_CODES, (
        f"AU BAS seed box codes {box_codes} != expected {_EXPECTED_BOX_CODES}"
    )

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["return_type"] == "BAS"
        assert r["feeder_tax_codes"], f"box {r['box_code']!r} has no feeder_tax_codes"
        # Round-trips through the real parser the service uses — this is
        # the same grammar check generate_return runs at call time, so a
        # malformed aggregation string fails here, in a fast unit test,
        # instead of surfacing as a 500 the first time someone runs a BAS
        # report against a freshly-seeded reference DB.
        parsed = _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r["feeder_tax_codes"],
                display_order=r["display_order"],
            )
        )
        assert parsed.box_code == r["box_code"]

    # The two GST-inclusive boxes must actually be inclusive, and the two
    # GST-exclusive ones must not — pins the exact semantics
    # services.tax_engine.au.bas_report already tests end-to-end.
    by_code = {r["box_code"]: r for r in rows}
    assert "gst_inclusive" in by_code["G1"]["aggregation"]
    assert "gst_inclusive" in by_code["G10"]["aggregation"]
    assert "gst_inclusive" in by_code["G11"]["aggregation"]
    assert "gst_exclusive" in by_code["G2"]["aggregation"]
    assert "gst_exclusive" in by_code["G3"]["aggregation"]
    assert by_code["1A"]["aggregation"] == "sum_tax_amount_for_codes:income"
    assert by_code["1B"]["aggregation"] == "sum_tax_amount_for_codes:purchase"
    assert set(by_code["1B"]["feeder_tax_codes"]) == {"taxable", "capital"}


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_tax_return_box_definitions() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t8")
    assert "AU/tax_return_box_definitions.yaml" in counts, (
        f"loader skipped the tax_return_box_definitions seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:  # type: ignore[union-attr]
        rows = (
            await s.execute(
                text(
                    "SELECT box_code, aggregation FROM tax_return_box_definitions "
                    "WHERE jurisdiction = 'AUS' AND return_type = 'BAS'"
                )
            )
        ).all()
        by_code = {r[0]: r[1] for r in rows}
        assert set(by_code) == _EXPECTED_BOX_CODES
        assert by_code["G1"] == "sum_taxable_for_codes:income:gst_inclusive"
        assert by_code["1A"] == "sum_tax_amount_for_codes:income"


# ---------------------------------------------------------------------------
# KMD-formula support Packet 2 — EE seed integrity (pure-unit, no DB).
# ---------------------------------------------------------------------------

_EE_KMD_EXPECTED_BOX_CODES = {
    "1", "1-1", "1-2", "2", "2-1", "2-2", "3", "3.1", "3.1.1", "3.2", "3.2.1",
    "4", "4-1", "5", "5.1", "5.2", "5.3", "5.4", "6", "6.1", "7", "7.1", "8",
    "9", "10", "11", "12", "13",
    # KMD-formula support Packet 3 (RC-FANOUT) + finding 1 (rate-aware RC
    # routing) — 8 internal-only ledger boxes feeding the box-1/2/2-2/5
    # BOX-FORMULA (see tax_return_box_definitions.yaml's header + box
    # comments). Not real EMTA box codes — never appear on the printed
    # KMD form. box 1_RC/2_RC/2-2_RC are rate-pinned (@24/@9/@13) so an
    # EU-acquisition reverse charge lands in the correct rate box.
    "1_DOMESTIC", "1_RC", "2_DOMESTIC", "2_RC",
    "2-2_DOMESTIC", "2-2_RC", "5_DOMESTIC", "5_RC",
}


def _load_ee_kmd_rows() -> list[dict]:
    doc = yaml.safe_load(_EE_SEED.read_text())
    assert doc["table"] == "tax_return_box_definitions"
    rows = [r for r in doc["rows"] if r["return_type"] == "KMD"]
    assert rows, "EE KMD seed rows not found"
    return rows


def test_ee_kmd_box_definitions_seed_valid() -> None:
    """Every EE KMD row is well-formed and round-trips through the real
    parser — the same integrity check the AU test above runs, extended
    to cover the ``formula`` aggregation kind Packet 2 introduces for EE
    boxes 4/12/13."""
    rows = _load_ee_kmd_rows()

    keys = [(r["jurisdiction"], r["return_type"], r["box_code"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, return_type, box_code) in EE KMD seed"

    box_codes = {r["box_code"] for r in rows}
    assert box_codes == _EE_KMD_EXPECTED_BOX_CODES, (
        f"EE KMD seed box codes {box_codes} != expected {_EE_KMD_EXPECTED_BOX_CODES}"
    )

    for r in rows:
        assert r["jurisdiction"] == "EST"
        parsed = _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r.get("feeder_tax_codes") or [],
                display_order=r["display_order"],
                formula=r.get("formula"),
            )
        )
        assert parsed.box_code == r["box_code"]
        if r["aggregation"] == "formula":
            assert parsed.kind == "formula"
            assert parsed.formula == r["formula"]
        elif r["aggregation"] != "manual":
            # CODES-NOW / CODES-SEED boxes must carry at least one
            # feeder_tax_codes entry — an aggregation box with no feeder
            # would silently always report 0, which is what "manual"
            # means to say honestly.
            assert r.get("feeder_tax_codes"), (
                f"EE KMD box {r['box_code']!r} has aggregation "
                f"{r['aggregation']!r} but no feeder_tax_codes"
            )


def test_ee_kmd_sub_box_feeders_are_contained_in_their_parent() -> None:
    """Every dotted sub-total's feeder_tax_codes must be a subset of its
    parent box's feeder_tax_codes — otherwise the sub-total could report
    more than the total it is meant to be "of" (advisor-flagged: box 5.1
    was wired without threading "input_import" back into box 5's own
    feeder list, which would have silently under-stated box 5 — and
    therefore over-stated box 12 payable — the moment a company posts an
    import-VAT line; box 5.1 is postable today, not gated on Packet 3).
    Generic guard over the whole sub-total family, not just the one
    instance that was caught by hand."""
    by_code = {r["box_code"]: r for r in _load_ee_kmd_rows()}

    parent_child = [
        ("3", "3.1"), ("3", "3.1.1"), ("3", "3.2"), ("3", "3.2.1"),
        ("3.1", "3.1.1"), ("3.2", "3.2.1"),
        # Packet 3: box "5" itself is now a BOX-FORMULA ("KMD:5_DOMESTIC +
        # KMD:5_RC") and no longer carries feeder_tax_codes directly — its
        # domestic-side feeder list (the thing 5.1/5.2, both exclusively
        # domestic import/capital sub-totals, must stay a subset of) now
        # lives on "5_DOMESTIC" (the exact pre-Packet-3 box-5 recipe,
        # unchanged) — see tax_return_box_definitions.yaml's box-5 comment.
        ("5_DOMESTIC", "5.1"), ("5_DOMESTIC", "5.2"),
        ("6", "6.1"), ("7", "7.1"),
    ]
    for parent_code, child_code in parent_child:
        parent_feeders = set(by_code[parent_code].get("feeder_tax_codes") or [])
        child_feeders = set(by_code[child_code].get("feeder_tax_codes") or [])
        assert child_feeders <= parent_feeders, (
            f"EE KMD box {child_code!r} feeder_tax_codes {child_feeders} is "
            f"not a subset of parent box {parent_code!r}'s "
            f"{parent_feeders} — {child_code!r} could report more than "
            f"the total it is 'of'"
        )


def test_ee_kmd_packet2_flipped_boxes_are_no_longer_manual() -> None:
    """Pins the exact Packet 2 disposition: the 11 CODES-SEED boxes are
    no longer manual, the 3 formula boxes carry a parseable expression,
    and the 5 boxes staying manual by design are unchanged."""
    by_code = {r["box_code"]: r for r in _load_ee_kmd_rows()}

    codes_seed = ["3.1", "3.1.1", "3.2", "3.2.1", "5.1", "5.2", "6", "6.1", "7", "7.1", "9"]
    for box_code in codes_seed:
        row = by_code[box_code]
        assert row["aggregation"] != "manual", f"box {box_code!r} should have flipped off manual"
        assert row["aggregation"].startswith(("sum_taxable_for_codes:", "sum_tax_amount_for_codes:")), (
            f"box {box_code!r} has unexpected aggregation {row['aggregation']!r}"
        )
        assert row.get("feeder_tax_codes"), f"box {box_code!r} has no feeder_tax_codes"

    formula_boxes = ["4", "12", "13"]
    for box_code in formula_boxes:
        row = by_code[box_code]
        assert row["aggregation"] == "formula"
        assert row.get("formula"), f"box {box_code!r} has no formula expression"

    still_manual = ["4-1", "10", "11", "5.3", "5.4"]
    for box_code in still_manual:
        row = by_code[box_code]
        assert row["aggregation"] == "manual", f"box {box_code!r} should stay manual by design"
        assert not row.get("formula"), f"box {box_code!r} is manual but carries a formula value"


def test_ee_kmd_box_4_is_the_scopes_rate_formula() -> None:
    by_code = {r["box_code"]: r for r in _load_ee_kmd_rows()}
    assert by_code["4"]["formula"] == (
        "0.24*KMD:1 + 0.20*KMD:1-1 + 0.22*KMD:1-2 + 0.09*KMD:2 + 0.05*KMD:2-1 + 0.13*KMD:2-2"
    )


def test_ee_kmd_box_12_13_are_the_scopes_max_zero_split() -> None:
    by_code = {r["box_code"]: r for r in _load_ee_kmd_rows()}
    assert by_code["12"]["formula"] == "max(0, KMD:4 + KMD:4-1 - KMD:5 + KMD:10 - KMD:11)"
    assert by_code["13"]["formula"] == "max(0, -(KMD:4 + KMD:4-1 - KMD:5 + KMD:10 - KMD:11))"


def test_ee_kmd_box_1_and_5_are_rc_fanout_formulas() -> None:
    """KMD-formula support Packet 3 (RC-FANOUT) — boxes 1 and 5 flip from
    CODES-NOW to BOX-FORMULA, each summing an internal "*_DOMESTIC"
    (unchanged pre-Packet-3 recipe) and "*_RC" (new role-based) pair."""
    by_code = {r["box_code"]: r for r in _load_ee_kmd_rows()}

    assert by_code["1"]["aggregation"] == "formula"
    assert by_code["1"]["formula"] == "KMD:1_DOMESTIC + KMD:1_RC"
    assert by_code["5"]["aggregation"] == "formula"
    assert by_code["5"]["formula"] == "KMD:5_DOMESTIC + KMD:5_RC"

    # "*_DOMESTIC" reproduces the exact pre-Packet-3 recipe (box 1 used
    # to be sum_taxable_for_codes:income:gst_exclusive over ["standard"];
    # box 5 used to be sum_tax_amount_for_codes:purchase over the long
    # historical-vintage feeder list) — byte-identical, just renamed.
    assert by_code["1_DOMESTIC"]["aggregation"] == "sum_taxable_for_codes:income:gst_exclusive"
    assert by_code["1_DOMESTIC"]["feeder_tax_codes"] == ["standard"]
    assert by_code["5_DOMESTIC"]["aggregation"] == "sum_tax_amount_for_codes:purchase"
    assert set(by_code["5_DOMESTIC"]["feeder_tax_codes"]) == {
        "standard", "standard_legacy_20", "standard_legacy_22",
        "reduced_13", "reduced_9", "capital", "input_import",
    }

    # "*_RC" uses the role-based bucket kind, keyed off the SAME
    # reporting_type tags box 6/6.1 read (rc_eu_acq_goods /
    # rc_eu_acq_services). Finding 1: the OUTPUT-side legs are rate-pinned
    # (@24 → box 1, @9 → box 2, @13 → box 2-2) so the applied rate routes
    # the base to the right output box; the INPUT-side leg (box 5_RC) is
    # rate-agnostic — it sums the actual self-assessed VAT at any rate.
    assert by_code["1_RC"]["aggregation"] == "sum_taxable_for_codes:output@24:gst_exclusive"
    assert set(by_code["1_RC"]["feeder_tax_codes"]) == {"rc_eu_acq_goods", "rc_eu_acq_services"}
    assert by_code["2_RC"]["aggregation"] == "sum_taxable_for_codes:output@9:gst_exclusive"
    assert by_code["2-2_RC"]["aggregation"] == "sum_taxable_for_codes:output@13:gst_exclusive"
    assert by_code["5_RC"]["aggregation"] == "sum_tax_amount_for_codes:input"
    assert set(by_code["5_RC"]["feeder_tax_codes"]) == {"rc_eu_acq_goods", "rc_eu_acq_services"}


def test_ee_kmd_formulas_have_no_dangling_box_refs() -> None:
    """Every box_code a formula references must exist in the EE KMD box
    set — a formula pointing at a nonexistent box would already fail at
    _FormulaParser time (unknown box reference), but this test asserts
    it explicitly at seed-review time, over every formula box in one
    pass, rather than only discovering it the first time a return is
    generated."""
    rows = _load_ee_kmd_rows()
    known_codes = frozenset(r["box_code"] for r in rows)

    for r in rows:
        if r["aggregation"] != "formula":
            continue
        ast = _FormulaParser(
            r["formula"], return_type="KMD", known_codes=known_codes
        ).parse()
        refs = _formula_refs(ast)
        dangling = refs - known_codes
        assert not dangling, (
            f"EE KMD box {r['box_code']!r} formula {r['formula']!r} "
            f"references unknown box(es) {dangling}"
        )


def test_ee_kmd_box_set_is_acyclic() -> None:
    """The formula-box dependency graph must be a DAG — runs the same
    topological sort ``_evaluate_formula_boxes`` runs at return-
    generation time, over the real seed, at seed-review time instead."""
    rows = _load_ee_kmd_rows()
    known_codes = frozenset(r["box_code"] for r in rows)
    formula_codes = {r["box_code"] for r in rows if r["aggregation"] == "formula"}

    deps: dict[str, frozenset[str]] = {}
    for r in rows:
        if r["aggregation"] != "formula":
            continue
        ast = _FormulaParser(
            r["formula"], return_type="KMD", known_codes=known_codes
        ).parse()
        deps[r["box_code"]] = frozenset(_formula_refs(ast) & formula_codes)

    order = _topological_order(deps)
    assert set(order) == formula_codes
