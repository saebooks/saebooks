"""LV jurisdiction-module seed validation.

Pure-YAML checks (no DB): every LV seed file is classified (loader
table vs reference_seed:false module data), every PVN declaration row
parses through the real ``_parse_box_definition`` grammar, every
formula parses against the seed's own box-code set with no dangling
references and an acyclic formula graph (the UK/EE seed-test pattern),
the reverse-charge rows route to Latvia's dedicated declaration rows
(NOT the EE fold-into-the-domestic-box shape), and the corporate-tax
rows carry the distribution-based scopes (never a flat-rate-on-profit
shape).

Gated (REFERENCE_MIGRATION_DATABASE_URL): ``load_seeds("LV")`` loads
every LV loader seed idempotently.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.entity_structure import ENTITY_STRUCTURE_BUCKETS
from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _formula_refs,
    _FormulaParser,
    _parse_box_definition,
    _topological_order,
)

_LV_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "LV"
)

_LOADER_SEEDS = {
    "tax_codes.yaml": "tax_codes",
    "tax_return_box_definitions.yaml": "tax_return_box_definitions",
    "corporate_tax_rates.yaml": "corporate_tax_rates",
    "withholding_tables.yaml": "withholding_tables",
    "social_contribution_schemes.yaml": "social_contribution_schemes",
    "mandatory_contribution_rules.yaml": "mandatory_contribution_rules",
    "entity_structure_types.yaml": "entity_structure_types",
    "chart_template.yaml": "chart_template",
    "fiscal_year_definitions.yaml": "fiscal_year_definitions",
}
_MODULE_DATA_SEEDS = {
    "e_invoicing.yaml",
    "tax_identifiers.yaml",
}

# Statutory PVN declaration rows + the S/P computed intermediates.
_PVN_EXPECTED_BOX_CODES = {
    "40", "41", "41.1", "42", "42.1", "43", "44", "45", "45.1", "46",
    "47", "48", "48.1", "48.2", "49", "50", "51", "51.1", "52", "53",
    "53.1", "54", "55", "56", "56.1", "57", "60", "61", "62", "63",
    "64", "65", "66", "67", "S", "P", "70", "80",
}

# The company-side reporting_type convention tax_codes.yaml's header
# defines — every feeder in the box seed must come from this set.
_LV_REPORTING_TYPES = {
    "standard", "reduced_12", "reduced_5", "exempt", "capital",
    "rc_domestic_supply", "rc_domestic_acq",
    "rc_eu_acq_goods", "rc_eu_acq_services", "rc_third_country_services",
    "zero_ic_goods", "zero_export", "zero_services",
    "zero_fz", "zero_art42_16", "zero_not_free_circ", "zero_new_vehicles",
    "outside_scope_supply", "input_import",
}


def _load(name: str) -> dict:
    return yaml.safe_load((_LV_DIR / name).read_text())


def test_every_lv_seed_file_is_classified() -> None:
    names = {p.name for p in _LV_DIR.glob("*.yaml")}
    assert names == set(_LOADER_SEEDS) | _MODULE_DATA_SEEDS


def test_loader_seeds_shape_and_jurisdiction() -> None:
    for name, table in _LOADER_SEEDS.items():
        doc = _load(name)
        assert doc["table"] == table, name
        assert doc["key"], name
        rows = doc["rows"]
        assert rows, f"{name} has no rows"
        for row in rows:
            assert row["jurisdiction"] == "LVA", (
                f"{name}: jurisdiction must be the 3-char reference code "
                f"'LVA', got {row['jurisdiction']!r}"
            )
        keys = [tuple(row.get(k) for k in doc["key"]) for row in rows]
        assert len(keys) == len(set(keys)), f"duplicate natural key in {name}"


def test_module_data_seeds_marked_and_wellformed() -> None:
    for name in _MODULE_DATA_SEEDS:
        doc = _load(name)
        assert doc.get("reference_seed") is False, name
        assert doc.get("jurisdiction") == "LVA", name
        assert "table" not in doc, name


# ---------------------------------------------------------------------------
# PVN declaration box definitions.
# ---------------------------------------------------------------------------


def _pvn_rows() -> list[dict]:
    doc = _load("tax_return_box_definitions.yaml")
    rows = [r for r in doc["rows"] if r["return_type"] == "PVN"]
    assert rows, "LV PVN rows not found"
    return rows


def test_pvn_box_set_and_grammar() -> None:
    rows = _pvn_rows()
    box_codes = {r["box_code"] for r in rows}
    assert box_codes == _PVN_EXPECTED_BOX_CODES

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
            assert feeders <= _LV_REPORTING_TYPES, (
                f"box {r['box_code']!r} feeders {feeders - _LV_REPORTING_TYPES} "
                "not in the documented LV reporting_type convention"
            )


def test_pvn_reverse_charge_routing_is_the_latvian_shape() -> None:
    """LV routes RC via dedicated rows — pin the routing so an EE-style
    fold-into-box-41 regression fails loudly."""
    by_code = {r["box_code"]: r for r in _pvn_rows()}

    # Row 41 is domestic-only: a plain income bucket, feeders exactly
    # {standard} — the RC base must NOT appear in it.
    assert by_code["41"]["aggregation"] == "sum_taxable_for_codes:income:gst_exclusive"
    assert set(by_code["41"]["feeder_tax_codes"]) == {"standard"}

    # EU acquisitions: rate-pinned output-role bases in 50/51/51.1 ...
    for code, rate in (("50", "21"), ("51", "12"), ("51.1", "5")):
        assert by_code[code]["aggregation"] == (
            f"sum_taxable_for_codes:output@{rate}:gst_exclusive"
        ), code
        assert set(by_code[code]["feeder_tax_codes"]) == {
            "rc_eu_acq_goods", "rc_eu_acq_services",
        }, code
    # ... with the VAT via rate formulas and the deductible side in 64.
    assert by_code["55"]["formula"] == "0.21*PVN:50"
    assert by_code["56"]["formula"] == "0.12*PVN:51"
    assert by_code["56.1"]["formula"] == "0.05*PVN:51.1"
    assert by_code["64"]["aggregation"] == "sum_tax_amount_for_codes:input"
    assert set(by_code["64"]["feeder_tax_codes"]) == {
        "rc_eu_acq_goods", "rc_eu_acq_services",
    }

    # Third-country services: output tax in 54, deductible in 63.
    assert by_code["54"]["aggregation"] == "sum_tax_amount_for_codes:output"
    assert set(by_code["54"]["feeder_tax_codes"]) == {"rc_third_country_services"}
    assert by_code["63"]["aggregation"] == "sum_tax_amount_for_codes:input"
    assert set(by_code["63"]["feeder_tax_codes"]) == {"rc_third_country_services"}

    # rc_domestic_acq feeds NOTHING (parked — engine refuses the tag);
    # rc_domestic_supply feeds exactly row 41.1 (seller side).
    for r in _pvn_rows():
        feeders = set(r.get("feeder_tax_codes") or [])
        assert "rc_domestic_acq" not in feeders, r["box_code"]
        if "rc_domestic_supply" in feeders:
            assert r["box_code"] == "41.1"

    # Output VAT rate formulas tax the DOMESTIC rows only (the seller
    # never charges VAT on 41.1).
    assert by_code["52"]["formula"] == "0.21*PVN:41"
    assert by_code["53"]["formula"] == "0.12*PVN:42"
    assert by_code["53.1"]["formula"] == "0.05*PVN:42.1"


def test_pvn_totals_and_net_position_formulas() -> None:
    by_code = {r["box_code"]: r for r in _pvn_rows()}
    assert by_code["40"]["formula"] == (
        "PVN:41 + PVN:41.1 + PVN:42 + PVN:42.1 + PVN:43 + PVN:48.2 + PVN:49"
    )
    assert by_code["43"]["formula"] == (
        "PVN:44 + PVN:45 + PVN:45.1 + PVN:46 + PVN:47 + PVN:48 + PVN:48.1"
    )
    assert by_code["60"]["formula"] == (
        "PVN:61 + PVN:62 + PVN:63 + PVN:64 + PVN:65"
    )
    assert by_code["S"]["formula"] == (
        "PVN:52 + PVN:53 + PVN:53.1 + PVN:54 + PVN:55 + PVN:56 + PVN:56.1 + PVN:57"
    )
    assert by_code["P"]["formula"] == "PVN:60 - PVN:66 + PVN:67"
    # The payable/overpay split (exactly one non-zero, EE 12/13 shape).
    assert by_code["80"]["formula"] == "max(0, PVN:S - PVN:P)"
    assert by_code["70"]["formula"] == "max(0, PVN:P - PVN:S)"
    # Filer-entered rows stay manual.
    for code in ("57", "65", "66", "67"):
        assert by_code[code]["aggregation"] == "manual", code


def test_pvn_formulas_no_dangling_refs_and_acyclic() -> None:
    rows = _pvn_rows()
    known_codes = frozenset(r["box_code"] for r in rows)
    formula_codes = {r["box_code"] for r in rows if r["aggregation"] == "formula"}

    deps: dict[str, frozenset[str]] = {}
    for r in rows:
        if r["aggregation"] != "formula":
            continue
        ast = _FormulaParser(
            r["formula"], return_type="PVN", known_codes=known_codes
        ).parse()
        refs = _formula_refs(ast)
        assert refs <= known_codes, (
            f"box {r['box_code']!r} references unknown box(es) {refs - known_codes}"
        )
        deps[r["box_code"]] = frozenset(refs & formula_codes)

    order = _topological_order(deps)
    assert set(order) == formula_codes


def test_pvn1_pvn2_ddz_uin_rows_are_manual_structural_only() -> None:
    doc = _load("tax_return_box_definitions.yaml")
    listings = [
        r for r in doc["rows"] if r["return_type"] in ("PVN1", "PVN2", "DDZ", "UIN")
    ]
    assert {r["return_type"] for r in listings} == {"PVN1", "PVN2", "DDZ", "UIN"}
    assert {r["box_code"] for r in listings if r["return_type"] == "PVN1"} == {
        "I", "II", "III",
    }
    for r in listings:
        assert r["aggregation"] == "manual", (
            f"{r['return_type']} row {r['box_code']!r} must stay a manual "
            "structural row — no ledger shape exists for a listing annex"
        )


# ---------------------------------------------------------------------------
# Tax codes.
# ---------------------------------------------------------------------------


def test_lv_tax_code_rates_are_the_2026_set() -> None:
    doc = _load("tax_codes.yaml")
    by_code = {}
    for r in doc["rows"]:
        by_code.setdefault(r["code"], []).append(r)
    assert Decimal(str(by_code["STD"][0]["rate_percent"])) == Decimal("21")
    assert Decimal(str(by_code["RED12"][0]["rate_percent"])) == Decimal("12")
    assert Decimal(str(by_code["RED5"][0]["rate_percent"])) == Decimal("5")
    # The RC pair is discriminated goods/services from day one (the EE
    # single-RC_EU_ACQ-row ambiguity deliberately not reproduced).
    assert "RC_EU_GOODS" in by_code and "RC_EU_SERVICES" in by_code
    assert "RC_3C_SERVICES" in by_code
    for r in doc["rows"]:
        assert r["tax_family"] == "vat_gst", r["code"]


# ---------------------------------------------------------------------------
# Corporate tax — the distribution-based shape.
# ---------------------------------------------------------------------------


def test_lv_corporate_tax_is_distribution_based_with_2026_elective() -> None:
    doc = _load("corporate_tax_rates.yaml")
    by_scope = {r["entity_scope"]: r for r in doc["rows"]}
    assert Decimal(str(by_scope["retained_reinvested"]["rate_percent"])) == Decimal("0")
    assert Decimal(str(by_scope["distributed_profit"]["rate_percent"])) == Decimal("20")
    alt = by_scope["distributed_profit_alternative"]
    assert Decimal(str(alt["rate_percent"])) == Decimal("15")
    assert str(alt["effective_from"]) == "2026-01-01"


# ---------------------------------------------------------------------------
# Payroll parameter seeds.
# ---------------------------------------------------------------------------


def test_lv_iin_withholding_row_is_flat_monthly_25_5() -> None:
    doc = _load("withholding_tables.yaml")
    row = next(r for r in doc["rows"] if r["code"] == "lv_iin_salary_paye")
    p = row["parameters"]
    assert Decimal(str(p["rate_percent"])) == Decimal("25.5")
    assert Decimal(str(p["non_taxable_minimum_monthly"])) == Decimal("550.00")
    assert Decimal(str(p["dependant_allowance_monthly"])) == Decimal("250.00")
    # The annual bands are documented as ANNUAL-return figures, never a
    # payroll band — pin the threshold numbers so a drift is loud.
    assert Decimal(str(p["annual_band_1_upper"])) == Decimal("105300.00")
    assert Decimal(str(p["annual_band_2_rate_percent"])) == Decimal("33.0")
    assert Decimal(str(p["annual_additional_rate_threshold"])) == Decimal("200000.00")


def test_lv_vsaoi_rates_and_cap() -> None:
    doc = _load("social_contribution_schemes.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}
    assert Decimal(str(by_code["lv_vsaoi_employer"]["rate_percent"])) == Decimal("23.59")
    assert Decimal(str(by_code["lv_vsaoi_employee"]["rate_percent"])) == Decimal("10.50")
    assert Decimal(str(by_code["lv_vsaoi_employer"]["wage_base_cap"])) == Decimal("105300.00")
    assert Decimal(str(by_code["lv_solidarity_tax"]["rate_percent"])) == Decimal("25")


def test_lv_pillar_ii_redirect_dated_rows() -> None:
    doc = _load("mandatory_contribution_rules.yaml")
    rows = sorted(doc["rows"], key=lambda r: str(r["effective_from"]))
    assert [Decimal(str(r["rate_percent"])) for r in rows] == [
        Decimal("6"), Decimal("5"),
    ]
    # The redirect is state-side — neither employer nor employee pays on
    # top (the LV payroll engine has NO retirement components).
    assert {r["payer"] for r in rows} == {"state"}
    assert str(rows[1]["effective_from"]) == "2025-01-01"
    assert str(rows[1]["effective_to"]) == "2028-12-31"


# ---------------------------------------------------------------------------
# Entities / fiscal year / e-invoicing.
# ---------------------------------------------------------------------------


def test_lv_entity_structures_use_valid_buckets() -> None:
    doc = _load("entity_structure_types.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}
    for r in doc["rows"]:
        assert r["canonical_bucket"] in ENTITY_STRUCTURE_BUCKETS, r["code"]
    assert by_code["sia"]["canonical_bucket"] == "company_limited"
    assert by_code["as"]["canonical_bucket"] == "company_limited"
    assert by_code["ik"]["canonical_bucket"] == "sole_trader"
    assert by_code["ps"]["canonical_bucket"] == "partnership"
    assert by_code["ks"]["canonical_bucket"] == "partnership"
    assert by_code["filiale"]["canonical_bucket"] == "other"
    assert by_code["biedriba"]["canonical_bucket"] == "nonprofit"


def test_lv_fiscal_year_definition() -> None:
    doc = _load("fiscal_year_definitions.yaml")
    row = doc["rows"][0]
    assert row["jurisdiction"] == "LVA"
    assert (row["fy_start_month"], row["fy_start_day"]) == (1, 1)
    assert row["quarter_anchors"] == [1, 4, 7, 10]


def test_lv_einvoicing_b2b_mandate_is_2028_not_2026() -> None:
    """The key research correction: the B2B mandate was POSTPONED to
    2028-01-01 — pin it so a stale-2026 regression fails loudly."""
    doc = _load("e_invoicing.yaml")
    by_scope = {m["scope"]: m for m in doc["milestones"]}
    assert str(by_scope["b2g"]["mandatory_from"]) == "2025-01-01"
    assert str(by_scope["b2b"]["mandatory_from"]) == "2028-01-01"
    assert str(by_scope["b2b_voluntary"]["mandatory_from"]) == "2026-03-30"
    assert doc["standard"] == "EN16931"


# ---------------------------------------------------------------------------
# Gated: real idempotent load through the reference loader.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)
async def test_load_lv_seeds_idempotent() -> None:
    from saebooks.services.reference.loader import load_seeds

    counts1 = await load_seeds("LV", version_tag="test-lv-1")
    expected_files = {f"LV/{name}" for name in _LOADER_SEEDS}
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected LV seed files: "
        f"missing={expected_files - set(counts1)}"
    )
    counts2 = await load_seeds("LV", version_tag="test-lv-2")
    assert counts1 == counts2
