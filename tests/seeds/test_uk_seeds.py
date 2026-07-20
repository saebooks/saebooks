"""UK jurisdiction-module seed integrity + reference-DB load.

Mirrors tests/seeds/test_jurisdiction_loader_ee.py (loader round-trip,
reference-DB-gated) and test_tax_return_box_definitions_seed.py's EE
section (pure-unit grammar/shape checks over the real seed YAML — the
checks generate_return would otherwise only run at return time).
"""
from __future__ import annotations

import os
import re
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

_UK_SEED_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "UK"
)

# The loader-table seeds (table + key + rows) and the module-data files
# (reference_seed: false, loaded off disk by jurisdictions/uk).
_LOADER_SEEDS = {
    "tax_codes.yaml": "tax_codes",
    "tax_return_box_definitions.yaml": "tax_return_box_definitions",
    "withholding_tables.yaml": "withholding_tables",
    "social_contribution_schemes.yaml": "social_contribution_schemes",
    "mandatory_contribution_rules.yaml": "mandatory_contribution_rules",
    "corporate_tax_rates.yaml": "corporate_tax_rates",
    "entity_structure_types.yaml": "entity_structure_types",
    "chart_template.yaml": "chart_template",
    "statutory_account_frameworks.yaml": "statutory_account_frameworks",
    "fiscal_year_definitions.yaml": "fiscal_year_definitions",
}
_MODULE_DATA_SEEDS = {
    "thresholds.yaml",
    "vat_schemes.yaml",
    "e_invoicing.yaml",
    "ni_thresholds.yaml",
    "pension_auto_enrolment.yaml",
    "statutory_payments.yaml",
    "cis.yaml",
    "tax_identifiers.yaml",
    "mileage_rates.yaml",
    "corporation_tax_parameters.yaml",
}

_VAT100_EXPECTED_BOX_CODES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    # Internal ledger legs (display_order >= 100) — the EE _DOMESTIC/_RC
    # convention; never appear on the statutory return.
    "1_DOMESTIC", "1_RC", "4_DOMESTIC", "4_RC", "6_DOMESTIC", "6_RC_SERVICES",
}

# The company-side reporting_type convention tax_codes.yaml's header
# defines — every feeder in the box seed must come from this set.
_UK_REPORTING_TYPES = {
    "standard", "reduced", "zero", "exempt", "outside_scope",
    "rc_construction", "rc_construction_supply", "rc_services_intl",
    "pva_import", "xi_eu_acq_goods", "xi_eu_dispatch",
}


def _load(name: str) -> dict:
    return yaml.safe_load((_UK_SEED_DIR / name).read_text())


def test_every_uk_seed_file_is_classified() -> None:
    """No stray files: everything in the UK seed dir is either a loader
    seed or a declared module-data file (reference_seed: false) — the
    loader hard-fails on a YAML with no ``table``, so an unclassified
    file would break ``load_seeds('UK')``."""
    names = {p.name for p in _UK_SEED_DIR.glob("*.yaml")}
    assert names == set(_LOADER_SEEDS) | _MODULE_DATA_SEEDS


def test_loader_seeds_shape_and_jurisdiction() -> None:
    for name, table in _LOADER_SEEDS.items():
        doc = _load(name)
        assert doc["table"] == table, name
        assert doc["key"], name
        rows = doc["rows"]
        assert rows, f"{name} has no rows"
        for row in rows:
            assert row["jurisdiction"] == "GBR", (
                f"{name}: jurisdiction must be the 3-char reference code "
                f"'GBR', got {row['jurisdiction']!r}"
            )
        # Natural keys unique within the file (idempotent upsert safety).
        keys = [tuple(row.get(k) for k in doc["key"]) for row in rows]
        assert len(keys) == len(set(keys)), f"duplicate natural key in {name}"


def test_module_data_seeds_marked_and_wellformed() -> None:
    for name in _MODULE_DATA_SEEDS:
        doc = _load(name)
        assert doc.get("reference_seed") is False, (
            f"{name} must carry reference_seed: false so the loader "
            "skips it instead of erroring on a missing 'table'"
        )
        assert doc.get("jurisdiction") == "GBR", name
        assert "table" not in doc, name


# ---------------------------------------------------------------------------
# VAT100 box definitions.
# ---------------------------------------------------------------------------


def _vat100_rows() -> list[dict]:
    doc = _load("tax_return_box_definitions.yaml")
    rows = [r for r in doc["rows"] if r["return_type"] == "VAT100"]
    assert rows, "UK VAT100 rows not found"
    return rows


def test_vat100_box_set_and_grammar() -> None:
    rows = _vat100_rows()
    box_codes = {r["box_code"] for r in rows}
    assert box_codes == _VAT100_EXPECTED_BOX_CODES

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
            assert feeders <= _UK_REPORTING_TYPES, (
                f"box {r['box_code']!r} feeders {feeders - _UK_REPORTING_TYPES} "
                "not in the documented UK reporting_type convention"
            )

    by_code = {r["box_code"]: r for r in rows}
    # The statutory formula boxes: 3 = 1 + 2, 5 = |3 - 4|, and the
    # split boxes 1/4/6 summing their internal legs.
    assert by_code["3"]["formula"] == "VAT100:1 + VAT100:2"
    assert by_code["5"]["formula"] == (
        "max(0, VAT100:3 - VAT100:4) + max(0, VAT100:4 - VAT100:3)"
    )
    # The RC/PVA role-keyed legs (EE precedent — no @rate pin: VAT100
    # output boxes are not rate-split).
    assert by_code["1_RC"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert by_code["4_RC"]["aggregation"] == "sum_tax_amount_for_codes:input"
    assert by_code["2"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert set(by_code["2"]["feeder_tax_codes"]) == {"xi_eu_acq_goods"}
    # Internal legs stay off the statutory return (display_order >= 100).
    for code in ("1_DOMESTIC", "1_RC", "4_DOMESTIC", "4_RC", "6_DOMESTIC", "6_RC_SERVICES"):
        assert by_code[code]["display_order"] >= 100, code


def test_vat100_formulas_no_dangling_refs_and_acyclic() -> None:
    rows = _vat100_rows()
    known_codes = frozenset(r["box_code"] for r in rows)
    formula_codes = {r["box_code"] for r in rows if r["aggregation"] == "formula"}

    deps: dict[str, frozenset[str]] = {}
    for r in rows:
        if r["aggregation"] != "formula":
            continue
        ast = _FormulaParser(
            r["formula"], return_type="VAT100", known_codes=known_codes
        ).parse()
        refs = _formula_refs(ast)
        assert refs <= known_codes, (
            f"box {r['box_code']!r} references unknown box(es) {refs - known_codes}"
        )
        deps[r["box_code"]] = frozenset(refs & formula_codes)

    order = _topological_order(deps)
    assert set(order) == formula_codes


def test_itsa_rows_are_manual_placeholders_only() -> None:
    doc = _load("tax_return_box_definitions.yaml")
    itsa = [r for r in doc["rows"] if r["return_type"].startswith("ITSA")]
    assert {r["return_type"] for r in itsa} == {"ITSA_QU", "ITSA_FINAL"}
    for r in itsa:
        assert r["aggregation"] == "manual", (
            f"ITSA row {r['box_code']!r} must stay a manual structural "
            "placeholder (category breakdown is parked pending the HMRC "
            "ITSA API spec pull)"
        )


def test_tax_codes_report_box_keys_point_at_real_vat100_boxes() -> None:
    """Every ``VAT100:<box>`` key in tax_codes.yaml must reference a box
    that exists in the box-definition seed — the AU/EE cross-check."""
    box_codes = {r["box_code"] for r in _vat100_rows()}
    doc = _load("tax_codes.yaml")
    for row in doc["rows"]:
        for key in row.get("report_box_keys") or []:
            m = re.fullmatch(r"VAT100:(.+)", key)
            assert m, f"tax code {row['code']!r} report_box_key {key!r} malformed"
            assert m.group(1) in box_codes, (
                f"tax code {row['code']!r} references nonexistent VAT100 "
                f"box {m.group(1)!r}"
            )


def test_uk_vat_rates_and_flags() -> None:
    doc = _load("tax_codes.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}
    from decimal import Decimal

    assert Decimal(str(by_code["STD"]["rate_percent"])) == Decimal("20")
    assert Decimal(str(by_code["RED"]["rate_percent"])) == Decimal("5")
    assert Decimal(str(by_code["ZERO"]["rate_percent"])) == Decimal("0")
    for rc in ("RC_CONSTRUCTION", "RC_SERVICES_INTL", "PVA_IMPORT", "XI_EU_ACQ_GOODS"):
        assert by_code[rc]["reverse_charge"] is True, rc
        assert by_code[rc]["direction"] == "purchase", rc
    assert by_code["EXEMPT"]["input_credit_recoverable"] is False
    # Outside-scope feeds NO box.
    assert not by_code["OUT_OF_SCOPE"].get("report_box_keys")


# ---------------------------------------------------------------------------
# Reference-DB loader round-trip (gated, mirrors the EE loader test).
# ---------------------------------------------------------------------------

pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_uk_idempotent() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    counts1 = await load_seeds("UK", version_tag="test-uk-1")
    expected_files = {f"UK/{name}" for name in _LOADER_SEEDS}
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected UK seed files: missing={expected_files - set(counts1)}"
    )
    # Module-data files must be skipped (0 rows), not errored on.
    for name in _MODULE_DATA_SEEDS:
        assert counts1.get(f"UK/{name}") == 0, name

    counts2 = await load_seeds("UK", version_tag="test-uk-2")
    assert counts1 == counts2, "Idempotent re-run should report identical row counts"

    async with ReferenceMigrationSession() as s:
        # Directory name 'UK' -> reference code 'GBR' (the EE/EST TRAP).
        n_tax_codes = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'GBR'")
            )
        ).scalar_one()
        assert n_tax_codes == 15, f"Expected 15 UK tax codes, got {n_tax_codes}"

        n_boxes = (
            await s.execute(
                text(
                    "SELECT count(*) FROM tax_return_box_definitions "
                    "WHERE jurisdiction = 'GBR'"
                )
            )
        ).scalar_one()
        # 15 VAT100 (9 statutory + 6 internal legs) + 2 ITSA_QU + 1
        # ITSA_FINAL manual placeholders.
        assert n_boxes == 18, f"Expected 18 UK box definitions, got {n_boxes}"

        n_entities = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_structure_types "
                    "WHERE jurisdiction = 'GBR'"
                )
            )
        ).scalar_one()
        assert n_entities == 9, f"Expected 9 UK entity structure types, got {n_entities}"
