"""M1.5 · Wave 5-Income — income-tax reference tables (CGT event
catalogue, loss carry-over rules, thin-capitalisation parameters,
foreign-tax relief): seed integrity + AU-parity proof + reference-DB load.

Three test groups (same shape as ``test_coa_statutory_seed`` /
``test_income_corporate_capital_bik_seed``):
  * Pure-unit YAML checks (no DB) — the AU seeds parse, target the right
    tables, key on their unique constraints, have unique natural keys,
    and every enum-backed value is a real module-tuple member. Runs in
    the standard suite.
  * AU-parity unit checks — AU losses carry forward indefinitely with
    capital quarantined; the current AU thin-cap general row is the 30%
    tax-EBITDA fixed ratio test; AU FITO is an unbasketed ordinary
    credit with the AUD 1,000 de-minimis and no carry-forward.
  * Reference-DB integration (skipped unless the reference DB is
    configured, same gate as the loader tests) — the seeds load and the
    AU rows resolve.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.foreign_tax_relief_rule import (
    FOREIGN_TAX_RELIEF_METHODS,
)
from saebooks.models.reference.tax_loss_carryover_rule import LOSS_BASKETS
from saebooks.models.reference.thin_capitalisation_rule import (
    THIN_CAP_MECHANIC_TYPES,
)

_AU_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
)
_CGT_EVENT_SEED = _AU_DIR / "capital_gains_event_types.yaml"
_LOSS_SEED = _AU_DIR / "tax_loss_carryover_rules.yaml"
_THIN_CAP_SEED = _AU_DIR / "thin_capitalisation_rules.yaml"
_FTR_SEED = _AU_DIR / "foreign_tax_relief_rules.yaml"


def test_au_capital_gains_event_type_seed_valid() -> None:
    doc = yaml.safe_load(_CGT_EVENT_SEED.read_text())
    assert doc["table"] == "capital_gains_event_types"
    assert doc["key"] == ["jurisdiction", "code"]
    rows = doc["rows"]
    assert rows, "AU CGT-event seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate CGT event codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["name"]
        assert r["category"]
        assert r["statutory_reference"].startswith("ITAA 1997 s 104-")

    # The operative disposal event dispose_asset() will one day map to.
    assert "A1" in codes
    # The catalogue spans the Act's lettered groups A..L.
    assert {c[0] for c in codes} == set("ABCDEFGHIJKL")


def test_au_tax_loss_carryover_rule_seed_valid() -> None:
    doc = yaml.safe_load(_LOSS_SEED.read_text())
    assert doc["table"] == "tax_loss_carryover_rules"
    assert doc["key"] == ["jurisdiction", "taxpayer_type", "loss_basket", "effective_from"]
    rows = doc["rows"]
    assert rows, "AU loss-carryover seed is empty"

    keys = [
        (r["jurisdiction"], r["taxpayer_type"], r["loss_basket"], r["effective_from"])
        for r in rows
    ]
    assert len(keys) == len(set(keys)), "duplicate natural keys in AU loss-carryover seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["loss_basket"] in LOSS_BASKETS, (
            f"row {r['taxpayer_type']}/{r['loss_basket']!r} has an unknown loss basket"
        )
        # AU parity: losses carry forward indefinitely, never back.
        assert r["carry_forward_years"] is None, (
            f"AU {r['taxpayer_type']}/{r['loss_basket']} row caps carry-forward — "
            "AU losses carry forward indefinitely"
        )
        assert r["carry_back_years"] == 0
        # AU parity: capital losses are quarantined, revenue losses are not.
        assert r["quarantined_to_basket"] is (r["loss_basket"] == "capital")

    by_tp_basket = {(r["taxpayer_type"], r["loss_basket"]): r for r in rows}
    # AU company revenue losses are COT/BCT-gated; individuals face no
    # continuity test.
    assert by_tp_basket[("company", "revenue")]["continuity_tests"] == [
        "continuity_of_ownership",
        "business_continuity",
    ]
    assert "continuity_tests" not in by_tp_basket[("individual", "revenue")]


def test_au_thin_capitalisation_rule_seed_valid() -> None:
    doc = yaml.safe_load(_THIN_CAP_SEED.read_text())
    assert doc["table"] == "thin_capitalisation_rules"
    assert doc["key"] == ["jurisdiction", "entity_scope", "mechanic_type", "effective_from"]
    rows = doc["rows"]
    assert rows, "AU thin-cap seed is empty"

    keys = [
        (r["jurisdiction"], r["entity_scope"], r["mechanic_type"], r["effective_from"])
        for r in rows
    ]
    assert len(keys) == len(set(keys)), "duplicate natural keys in AU thin-cap seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["mechanic_type"] in THIN_CAP_MECHANIC_TYPES, (
            f"row {r['entity_scope']}/{r['mechanic_type']!r} has an unknown mechanic"
        )
        # AU parity: the AUD 2m de-minimis carves small entities out of
        # every arm of the regime.
        assert float(r["de_minimis_threshold"]) == 2000000.00

    # AU parity: the CURRENT general rule is the post-2023 fixed ratio
    # test (30% of tax EBITDA, group-ratio election, 15-year
    # carry-forward of denied amounts) ...
    frt = next(r for r in rows if r["mechanic_type"] == "fixed_ratio_ebitda")
    assert frt["entity_scope"] == "general"
    assert float(frt["fixed_ratio_pct"]) == 30.0
    assert frt["ratio_base"] == "tax_ebitda"
    assert frt["group_ratio_election_allowed"] is True
    assert frt["disallowed_carryforward_years"] == 15
    assert "effective_to" not in frt

    # ... and the pre-2023 general safe harbour is closed out, not deleted.
    old = next(
        r for r in rows
        if r["entity_scope"] == "general"
        and r["mechanic_type"] == "safe_harbour_debt_ratio"
    )
    assert old["effective_to"] is not None


def test_au_foreign_tax_relief_rule_seed_valid() -> None:
    doc = yaml.safe_load(_FTR_SEED.read_text())
    assert doc["table"] == "foreign_tax_relief_rules"
    assert doc["key"] == ["jurisdiction", "taxpayer_type", "income_basket", "effective_from"]
    rows = doc["rows"]
    assert rows, "AU foreign-tax-relief seed is empty"

    keys = [
        (r["jurisdiction"], r["taxpayer_type"], r["income_basket"], r["effective_from"])
        for r in rows
    ]
    assert len(keys) == len(set(keys)), "duplicate natural keys in AU foreign-tax-relief seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["relief_method"] in FOREIGN_TAX_RELIEF_METHODS, (
            f"row effective {r['effective_from']} has an unknown relief method"
        )

    # AU parity: current FITO = unbasketed ordinary credit, AUD 1,000
    # de-minimis, excess offset lost (no carry-forward or carry-back).
    fito = next(r for r in rows if "effective_to" not in r)
    assert fito["relief_method"] == "ordinary_credit"
    assert fito["income_basket"] == "any"
    assert float(fito["offset_de_minimis_amount"]) == 1000.00
    assert fito["carry_forward_years"] == 0
    assert fito["carry_back_years"] == 0
    assert fito["limitation_formula"] == {
        "limit": "domestic_tax_on_double_taxed_amounts"
    }


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_income_tax_reference_seeds() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-m15-income")
    for fname in (
        "AU/capital_gains_event_types.yaml",
        "AU/tax_loss_carryover_rules.yaml",
        "AU/thin_capitalisation_rules.yaml",
        "AU/foreign_tax_relief_rules.yaml",
    ):
        assert fname in counts, f"loader skipped {fname}: {sorted(counts)}"

    async with ReferenceMigrationSession() as s:
        # The A1 disposal event exists in the catalogue.
        a1 = (
            await s.execute(
                text(
                    "SELECT name FROM capital_gains_event_types "
                    "WHERE jurisdiction = 'AUS' AND code = 'A1'"
                )
            )
        ).scalar_one()
        assert a1 == "Disposal of a CGT asset"

        # AU company revenue losses carry forward indefinitely.
        cf = (
            await s.execute(
                text(
                    "SELECT carry_forward_years FROM tax_loss_carryover_rules "
                    "WHERE jurisdiction = 'AUS' AND taxpayer_type = 'company' "
                    "AND loss_basket = 'revenue'"
                )
            )
        ).scalar_one()
        assert cf is None

        # The current AU general thin-cap rule is the 30% fixed ratio test.
        frt = (
            await s.execute(
                text(
                    "SELECT fixed_ratio_pct FROM thin_capitalisation_rules "
                    "WHERE jurisdiction = 'AUS' AND entity_scope = 'general' "
                    "AND mechanic_type = 'fixed_ratio_ebitda' "
                    "AND effective_to IS NULL"
                )
            )
        ).scalar_one()
        assert float(frt) == 30.0

        # The current AU foreign-tax relief rule is the FITO ordinary credit.
        fito = (
            await s.execute(
                text(
                    "SELECT relief_method, offset_de_minimis_amount "
                    "FROM foreign_tax_relief_rules "
                    "WHERE jurisdiction = 'AUS' AND effective_to IS NULL"
                )
            )
        ).first()
        assert fito is not None
        assert fito.relief_method == "ordinary_credit"
        assert float(fito.offset_de_minimis_amount) == 1000.00
