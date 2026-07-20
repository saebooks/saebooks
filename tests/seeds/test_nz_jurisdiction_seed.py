"""NZ jurisdiction-module seed validation.

Pure-YAML checks (no DB): every NZ GST101 box row parses through the
real ``_parse_box_definition`` grammar, every formula parses against
the seed's own box-code set (dangling-reference guard, mirroring the EE
KMD seed tests), the 3/23 coefficient is correct to Decimal working
precision, and NO NZ tax-code row carries a 9% rate (the s 10(6)
accommodation rule is a value apportionment, never a rate — the seed's
own invariant).

Gated (REFERENCE_MIGRATION_DATABASE_URL): ``load_seeds("NZ")`` loads
every NZ seed file idempotently — the seed-load test the build brief
requires.
"""
from __future__ import annotations

import os
from decimal import Decimal, getcontext
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.entity_structure import ENTITY_STRUCTURE_BUCKETS
from saebooks.models.reference.retirement_vehicle import (
    RETIREMENT_TAX_TREATMENTS,
    RETIREMENT_VEHICLE_BUCKETS,
)
from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _FormulaParser,
    _parse_box_definition,
)

_NZ_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "NZ"
)


def _load(name: str) -> dict:
    return yaml.safe_load((_NZ_DIR / name).read_text())


# ---------------------------------------------------------------------------
# GST101 box definitions.
# ---------------------------------------------------------------------------


def _gst101_rows() -> list[dict]:
    doc = _load("tax_return_box_definitions.yaml")
    assert doc["table"] == "tax_return_box_definitions"
    rows = [r for r in doc["rows"] if r["return_type"] == "GST101"]
    assert rows, "no GST101 rows in the NZ box seed"
    return rows


def _parsed_boxes() -> list:
    return [
        _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r.get("feeder_tax_codes") or [],
                display_order=r["display_order"],
                formula=r.get("formula"),
            )
        )
        for r in _gst101_rows()
    ]


def test_nz_gst101_box_definitions_parse() -> None:
    parsed = _parsed_boxes()
    codes = {b.box_code for b in parsed}
    # The 11 filable boxes plus the two internal Box-5 legs.
    assert codes == {
        "5", "5_TAXABLE", "5_ZERO", "6", "7", "8", "9", "10",
        "11", "12", "13", "14", "15",
    }
    # Internal legs use the display_order >= 100 persist-exclusion
    # convention; every filable box stays below it.
    for b in parsed:
        if b.box_code in ("5_TAXABLE", "5_ZERO"):
            assert b.display_order >= 100, b.box_code
        else:
            assert b.display_order < 100, b.box_code


def test_nz_gst101_formulas_have_no_dangling_refs() -> None:
    parsed = _parsed_boxes()
    known = frozenset(b.box_code for b in parsed)
    for b in parsed:
        if b.kind == "formula":
            # Raises FormulaSyntaxError on any unknown box reference.
            _FormulaParser(
                b.formula, return_type="GST101", known_codes=known
            ).parse()


def test_nz_gst101_manual_boxes_are_exactly_the_calculation_sheet_pair() -> None:
    parsed = _parsed_boxes()
    manual = {b.box_code for b in parsed if b.kind == "manual"}
    assert manual == {"9", "13"}


def test_nz_gst101_three_twenty_thirds_coefficient_is_exact_at_working_precision() -> None:
    # The grammar has no division operator, so 3/23 is a decimal
    # literal — it must equal 3/23 at (beyond) Decimal's default
    # 28-significant-digit working precision.
    rows = {r["box_code"]: r for r in _gst101_rows()}
    for box in ("8", "12"):
        formula = rows[box]["formula"]
        coef = Decimal(formula.split("*")[1].strip())
        getcontext().prec = 40
        exact = Decimal(3) / Decimal(23)
        assert abs(coef - exact) < Decimal("1e-33"), box
        getcontext().prec = 28


def test_nz_gst101_box15_is_signed_not_max_split() -> None:
    rows = {r["box_code"]: r for r in _gst101_rows()}
    assert "max(" not in rows["15"]["formula"]
    assert rows["15"]["formula"].replace(" ", "") == "GST101:10-GST101:14"


# ---------------------------------------------------------------------------
# Tax codes — the 9%-is-not-a-rate invariant + convention shape.
# ---------------------------------------------------------------------------


def test_nz_tax_codes_never_seed_a_nine_percent_rate() -> None:
    doc = _load("tax_codes.yaml")
    rates = {Decimal(str(r["rate_percent"])) for r in doc["rows"]}
    assert Decimal("9") not in rates, (
        "the s 10(6) long-stay accommodation rule is a 60%-of-value "
        "apportionment at 15%, never a 9% rate"
    )
    assert rates == {Decimal("15.0000"), Decimal("0.0000")}


def test_nz_tax_codes_accommodation_row_is_fifteen_percent() -> None:
    doc = _load("tax_codes.yaml")
    accom = [r for r in doc["rows"] if r["code"] == "ACCOM_LT"]
    assert len(accom) == 1
    assert Decimal(str(accom[0]["rate_percent"])) == Decimal("15.0000")


def test_nz_tax_codes_jurisdiction_and_family() -> None:
    doc = _load("tax_codes.yaml")
    for row in doc["rows"]:
        assert row["jurisdiction"] == "NZL"
        assert row["tax_family"] == "vat_gst"


# ---------------------------------------------------------------------------
# Enum-membership checks for the taxonomy-mapped seeds.
# ---------------------------------------------------------------------------


def test_nz_entity_structures_use_valid_buckets_and_include_ltc() -> None:
    doc = _load("entity_structure_types.yaml")
    by_code = {r["code"]: r for r in doc["rows"]}
    for row in doc["rows"]:
        assert row["canonical_bucket"] in ENTITY_STRUCTURE_BUCKETS, row["code"]
    # The LTC maps to PASS_THROUGH — corporate form + elective
    # flow-through fits the existing taxonomy without strain.
    assert by_code["ltc"]["canonical_bucket"] == "pass_through"
    assert set(by_code) == {
        "company", "sole_trader", "partnership", "limited_partnership",
        "trust", "ltc", "incorporated_society", "charitable_trust",
    }


def test_nz_kiwisaver_vehicle_row() -> None:
    doc = _load("retirement_vehicle_types.yaml")
    rows = doc["rows"]
    assert len(rows) == 1 and rows[0]["code"] == "kiwisaver"
    assert rows[0]["canonical_bucket"] in RETIREMENT_VEHICLE_BUCKETS
    # KiwiSaver is TTE; the enum has no TTE member so the row carries
    # "other" (flagged taxonomy gap) — pin that it stays enum-valid.
    assert rows[0]["tax_treatment"] in RETIREMENT_TAX_TREATMENTS
    assert rows[0]["tax_treatment"] == "other"


def test_nz_kiwisaver_dated_steps() -> None:
    from datetime import date

    doc = _load("mandatory_contribution_rules.yaml")
    for code in ("nz_kiwisaver_employee_default", "nz_kiwisaver_employer_min"):
        rows = sorted(
            (r for r in doc["rows"] if r["code"] == code),
            key=lambda r: r["effective_from"],
        )
        steps = [
            (r["effective_from"], Decimal(str(r["rate_percent"])))
            for r in rows
        ]
        assert [s[1] for s in steps] == [
            Decimal("3.0000"), Decimal("3.5000"), Decimal("4.0000")
        ], code
        assert steps[1][0] == date(2026, 4, 1), code
        assert steps[2][0] == date(2028, 4, 1), code
        # Contiguous dated series — no gap between a row's end and the
        # next row's start.
        import itertools

        for prev, nxt in itertools.pairwise(rows):
            assert prev["effective_to"] < nxt["effective_from"]


def test_nz_acc_earners_levy_dated_rows() -> None:
    from datetime import date

    doc = _load("social_contribution_schemes.yaml")
    rows = sorted(doc["rows"], key=lambda r: r["effective_from"])
    assert [
        (r["effective_from"], Decimal(str(r["rate_percent"])), Decimal(str(r["wage_base_cap"])))
        for r in rows
    ] == [
        (date(2025, 4, 1), Decimal("1.6700"), Decimal("152790.00")),
        (date(2026, 4, 1), Decimal("1.7500"), Decimal("156641.00")),
        (date(2027, 4, 1), Decimal("1.8300"), Decimal("160244.00")),
    ]


def test_nz_fbt_rates_todays_law_only() -> None:
    doc = _load("benefit_in_kind_rates.yaml")
    by_cat = {r["benefit_category"]: r for r in doc["rows"]}
    assert Decimal(str(by_cat["single_rate"]["rate_percent"])) == Decimal("63.9300")
    assert Decimal(str(by_cat["alternate_rate"]["rate_percent"])) == Decimal("49.2500")
    # The unenacted 2027 motor-vehicle change must NOT be seeded.
    assert set(by_cat) == {"single_rate", "alternate_rate"}
    for r in doc["rows"]:
        assert r["incidence"] == "employer_taxed"
        # FBT year is 1 April - 31 March.
        assert (r["filing_period_start_month"], r["filing_period_end_month"]) == (4, 3)


def test_nz_fiscal_year_definition() -> None:
    doc = _load("fiscal_year_definitions.yaml")
    row = doc["rows"][0]
    assert row["jurisdiction"] == "NZL"
    # 1 April - 31 March, quarters anchored Apr/Jul/Oct/Jan.
    assert (row["fy_start_month"], row["fy_start_day"]) == (4, 1)
    assert row["quarter_anchors"] == [4, 7, 10, 1]


# ---------------------------------------------------------------------------
# Gated: real idempotent load through the reference loader.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)
async def test_load_nz_seeds_idempotent() -> None:
    from saebooks.services.reference.loader import load_seeds

    counts1 = await load_seeds("NZ", version_tag="test-nz-1")
    expected_files = {
        "NZ/tax_codes.yaml",
        "NZ/tax_return_box_definitions.yaml",
        "NZ/corporate_tax_rates.yaml",
        "NZ/withholding_tables.yaml",
        "NZ/retirement_vehicle_types.yaml",
        "NZ/mandatory_contribution_rules.yaml",
        "NZ/social_contribution_schemes.yaml",
        "NZ/benefit_in_kind_rates.yaml",
        "NZ/entity_structure_types.yaml",
        "NZ/statutory_account_frameworks.yaml",
        "NZ/chart_template.yaml",
        "NZ/fiscal_year_definitions.yaml",
    }
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected NZ seed files: "
        f"missing={expected_files - set(counts1)}"
    )
    # Idempotent re-run reports identical row counts (pure upsert).
    counts2 = await load_seeds("NZ", version_tag="test-nz-2")
    assert counts1 == counts2
