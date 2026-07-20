"""LT jurisdiction-module seed integrity + reference-DB load.

Mirrors tests/seeds/test_uk_seeds.py (which mirrors the EE loader
round-trip + the pure-unit grammar/shape checks over the real seed
YAML — the checks generate_return would otherwise only run at return
time).
"""
from __future__ import annotations

import os
import re
from decimal import Decimal
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

_LT_SEED_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "LT"
)

_LOADER_SEEDS = {
    "tax_codes.yaml": "tax_codes",
    "tax_return_box_definitions.yaml": "tax_return_box_definitions",
    "withholding_tables.yaml": "withholding_tables",
    "social_contribution_schemes.yaml": "social_contribution_schemes",
    "mandatory_contribution_rules.yaml": "mandatory_contribution_rules",
    "corporate_tax_rates.yaml": "corporate_tax_rates",
    "entity_structure_types.yaml": "entity_structure_types",
    "chart_template.yaml": "chart_template",
    "fiscal_year_definitions.yaml": "fiscal_year_definitions",
}
_MODULE_DATA_SEEDS = {
    "e_invoicing.yaml",
    "tax_identifiers.yaml",
}

_FR0600_EXPECTED_BOX_CODES = {
    # Part I supplies
    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
    # Part II acquisitions
    "21", "22", "23", "24",
    # Part III input/import VAT + Part IV pro-rata
    "25", "26", "27", "28",
    # Part V output VAT + result (29A is the FR0600(3) 12%-rate box)
    "29", "29A", "30", "31", "32", "33", "34", "35", "36",
    # Internal ledger legs (display_order >= 100, EE convention)
    "25_DOMESTIC", "25_RC",
}

# The company-side reporting_type convention tax_codes.yaml's header
# defines — every feeder in the box seed must come from this set.
_LT_REPORTING_TYPES = {
    "standard", "reduced_12", "reduced_9_legacy", "reduced_5",
    "exempt", "zero_export", "zero_ic_goods", "zero_other",
    "outside_lt", "out_of_scope",
    "rc_domestic_supply", "rc_domestic_acq",
    "rc_eu_acq_goods", "rc_eu_acq_services", "rc_services_noneu",
    "input_import",
}


def _load(name: str) -> dict:
    return yaml.safe_load((_LT_SEED_DIR / name).read_text())


def test_every_lt_seed_file_is_classified() -> None:
    names = {p.name for p in _LT_SEED_DIR.glob("*.yaml")}
    assert names == set(_LOADER_SEEDS) | _MODULE_DATA_SEEDS


def test_loader_seeds_shape_and_jurisdiction() -> None:
    for name, table in _LOADER_SEEDS.items():
        doc = _load(name)
        assert doc["table"] == table, name
        assert doc["key"], name
        rows = doc["rows"]
        assert rows, f"{name} has no rows"
        for row in rows:
            assert row["jurisdiction"] == "LTU", (
                f"{name}: jurisdiction must be the 3-char reference code "
                f"'LTU', got {row['jurisdiction']!r}"
            )
        keys = [tuple(row.get(k) for k in doc["key"]) for row in rows]
        assert len(keys) == len(set(keys)), f"duplicate natural key in {name}"


def test_module_data_seeds_marked_and_wellformed() -> None:
    for name in _MODULE_DATA_SEEDS:
        doc = _load(name)
        assert doc.get("reference_seed") is False, name
        assert doc.get("jurisdiction") == "LTU", name
        assert "table" not in doc, name


# ---------------------------------------------------------------------------
# FR0600 box definitions.
# ---------------------------------------------------------------------------


def _fr0600_rows() -> list[dict]:
    doc = _load("tax_return_box_definitions.yaml")
    rows = [r for r in doc["rows"] if r["return_type"] == "FR0600"]
    assert rows, "LT FR0600 rows not found"
    return rows


def test_fr0600_box_set_and_grammar() -> None:
    rows = _fr0600_rows()
    box_codes = {r["box_code"] for r in rows}
    assert box_codes == _FR0600_EXPECTED_BOX_CODES

    for r in rows:
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
        if r["aggregation"] not in ("formula", "manual"):
            feeders = set(r.get("feeder_tax_codes") or [])
            assert feeders, f"box {r['box_code']!r} has no feeder_tax_codes"
            assert feeders <= _LT_REPORTING_TYPES, (
                f"box {r['box_code']!r} feeders {feeders - _LT_REPORTING_TYPES} "
                "not in the documented LT reporting_type convention"
            )

    by_code = {r["box_code"]: r for r in rows}
    # The formula boxes: 25 = domestic + RC legs; 35 = 25 + 26 + 27;
    # 36 = the SIGNED result (box 27 on both sides — cancels).
    assert by_code["25"]["formula"] == "FR0600:25_DOMESTIC + FR0600:25_RC"
    assert by_code["35"]["formula"] == "FR0600:25 + FR0600:26 + FR0600:27"
    assert by_code["36"]["formula"] == (
        "FR0600:29 + FR0600:29A + FR0600:30 + FR0600:31 + FR0600:32 + "
        "FR0600:33 + FR0600:34 + FR0600:27 - FR0600:35"
    )
    # Role-keyed self-assessment boxes (NOT rate-split — no @rate pin,
    # the VAT100 posture, contrast EE KMD).
    assert by_code["32"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert by_code["33"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert by_code["34"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert by_code["25_RC"]["aggregation"] == "sum_tax_amount_for_codes:input"
    # Rate-split OUTPUT-VAT boxes discriminate by tag, incl. the new
    # FR0600(3) 12% box.
    assert set(by_code["29"]["feeder_tax_codes"]) == {"standard"}
    assert set(by_code["29A"]["feeder_tax_codes"]) == {"reduced_12"}
    assert set(by_code["30"]["feeder_tax_codes"]) == {"reduced_9_legacy"}
    assert set(by_code["31"]["feeder_tax_codes"]) == {"reduced_5"}
    # Manual-by-design boxes.
    for code in ("14", "15", "16", "22", "27", "28"):
        assert by_code[code]["aggregation"] == "manual", code
    # Internal legs stay off the statutory return.
    for code in ("25_DOMESTIC", "25_RC"):
        assert by_code[code]["display_order"] >= 100, code


def test_fr0600_formulas_no_dangling_refs_and_acyclic() -> None:
    rows = _fr0600_rows()
    known_codes = frozenset(r["box_code"] for r in rows)
    formula_codes = {r["box_code"] for r in rows if r["aggregation"] == "formula"}

    deps: dict[str, frozenset[str]] = {}
    for r in rows:
        if r["aggregation"] != "formula":
            continue
        ast = _FormulaParser(
            r["formula"], return_type="FR0600", known_codes=known_codes
        ).parse()
        refs = _formula_refs(ast)
        assert refs <= known_codes, (
            f"box {r['box_code']!r} references unknown box(es) {refs - known_codes}"
        )
        deps[r["box_code"]] = frozenset(refs & formula_codes)

    order = _topological_order(deps)
    assert set(order) == formula_codes


def test_isaf_and_gpm313_rows_are_manual_placeholders_only() -> None:
    doc = _load("tax_return_box_definitions.yaml")
    other = [r for r in doc["rows"] if r["return_type"] != "FR0600"]
    assert {r["return_type"] for r in other} == {"ISAF", "GPM313"}
    for r in other:
        assert r["aggregation"] == "manual", (
            f"{r['return_type']} row {r['box_code']!r} must stay a manual "
            "structural placeholder (the i.SAF register exporter is a "
            "later phase — kmd_2027 precedent; GPM313 is a payroll data "
            "model, not a ledger box vector)"
        )


def test_tax_codes_report_box_keys_point_at_real_fr0600_boxes() -> None:
    box_codes = {r["box_code"] for r in _fr0600_rows()}
    doc = _load("tax_codes.yaml")
    for row in doc["rows"]:
        for key in row.get("report_box_keys") or []:
            m = re.fullmatch(r"FR0600:(.+)", key)
            assert m, f"tax code {row['code']!r} report_box_key {key!r} malformed"
            assert m.group(1) in box_codes, (
                f"tax code {row['code']!r} references nonexistent FR0600 "
                f"box {m.group(1)!r}"
            )


def test_lt_vat_rates_and_flags() -> None:
    doc = _load("tax_codes.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}

    # Standard rate 21% — UNCHANGED in 2026 (the load-bearing verdict:
    # the defence package reshuffled the REDUCED rates, not this one).
    assert Decimal(str(by_code["STD"]["rate_percent"])) == Decimal("21")
    # The 2026-01-01 reshuffle (Law XV-287): new 12%, legacy 9%, 5%.
    assert Decimal(str(by_code["RED12"]["rate_percent"])) == Decimal("12")
    assert str(by_code["RED12"]["effective_from"]) == "2026-01-01"
    assert Decimal(str(by_code["RED9"]["rate_percent"])) == Decimal("9")
    assert Decimal(str(by_code["RED5"]["rate_percent"])) == Decimal("5")
    for rc in ("RC_DOM_ACQ", "EU_ACQ_GOODS", "EU_ACQ_SERVICES", "RC_SERVICES_NONEU"):
        assert by_code[rc]["reverse_charge"] is True, rc
        assert by_code[rc]["direction"] == "purchase", rc
    assert by_code["EXEMPT"]["input_credit_recoverable"] is False
    # Out-of-scope feeds NO box; outside-LT feeds box 20 ONLY.
    assert not by_code["OUT_OF_SCOPE"].get("report_box_keys")
    assert by_code["OUTSIDE_LT"]["report_box_keys"] == ["FR0600:20"]
    # The seller side of the Art 96 RC feeds box 12 only, no VAT.
    assert by_code["RC_DOM_SUPPLY"]["report_box_keys"] == ["FR0600:12"]
    assert Decimal(str(by_code["RC_DOM_SUPPLY"]["rate_percent"])) == Decimal("0")


def test_lt_corporate_rates_2026() -> None:
    doc = _load("corporate_tax_rates.yaml")
    by_key = {(r["tax_year"], r["entity_scope"]): r for r in doc["rows"]}
    assert Decimal(str(by_key[(2025, "standard")]["rate_percent"])) == Decimal("16")
    assert Decimal(str(by_key[(2026, "standard")]["rate_percent"])) == Decimal("17")
    assert Decimal(str(by_key[(2025, "small_company")]["rate_percent"])) == Decimal("6")
    assert Decimal(str(by_key[(2026, "small_company")]["rate_percent"])) == Decimal("7")
    assert Decimal(str(by_key[(2026, "new_company_relief")]["rate_percent"])) == Decimal("0")


def test_lt_entity_types_cover_the_lt_specific_forms() -> None:
    doc = _load("entity_structure_types.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}
    assert set(by_code) == {
        "uab", "ab", "mb", "ii", "tub", "kub", "vsi", "asociacija", "filialas",
    }
    # MB — the LT-specific small partnership: limited liability +
    # entity-level CIT -> company_limited (the taxonomy-fit check the
    # build brief asked for, the NZ LTC precedent).
    assert by_code["mb"]["canonical_bucket"] == "company_limited"
    assert by_code["ii"]["canonical_bucket"] == "sole_trader"
    assert by_code["filialas"]["canonical_bucket"] == "other"


# ---------------------------------------------------------------------------
# Reference-DB loader round-trip (gated, mirrors the EE/UK loader tests).
# ---------------------------------------------------------------------------

pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_lt_idempotent() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    counts1 = await load_seeds("LT", version_tag="test-lt-1")
    expected_files = {f"LT/{name}" for name in _LOADER_SEEDS}
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected LT seed files: missing={expected_files - set(counts1)}"
    )
    for name in _MODULE_DATA_SEEDS:
        assert counts1.get(f"LT/{name}") == 0, name

    counts2 = await load_seeds("LT", version_tag="test-lt-2")
    assert counts1 == counts2, "Idempotent re-run should report identical row counts"

    async with ReferenceMigrationSession() as s:
        # Directory name 'LT' -> reference code 'LTU' (the EE/EST TRAP).
        n_tax_codes = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'LTU'")
            )
        ).scalar_one()
        assert n_tax_codes == 21, f"Expected 21 LT tax codes, got {n_tax_codes}"

        n_boxes = (
            await s.execute(
                text(
                    "SELECT count(*) FROM tax_return_box_definitions "
                    "WHERE jurisdiction = 'LTU'"
                )
            )
        ).scalar_one()
        # 29 FR0600 (27 form boxes + 2 internal legs) + 2 ISAF + 1 GPM313.
        assert n_boxes == 32, f"Expected 32 LT box definitions, got {n_boxes}"

        n_entities = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_structure_types "
                    "WHERE jurisdiction = 'LTU'"
                )
            )
        ).scalar_one()
        assert n_entities == 9, f"Expected 9 LT entity structure types, got {n_entities}"
