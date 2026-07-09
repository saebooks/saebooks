"""M1.5 · T7 — canonical payroll-withholding & social-contribution seed
integrity + reference-DB load.

Four tests:
  * ``test_au_withholding_table_seed_valid`` — pure-unit check (no DB)
    that the AU withholding-tables seed has valid enum values and unique
    keys.
  * ``test_au_social_contribution_scheme_seed_valid`` — pure-unit check
    (no DB) that the AU social-contribution-schemes seed has valid enum
    values and unique keys.
  * ``test_load_withholding_tables`` — reference-DB integration (skipped
    unless the reference DB is configured, same gate as the loader test)
    proving the AU PAYG withholding table loads and resolves.
  * ``test_load_social_contribution_schemes`` — reference-DB integration
    proving the AU Medicare-levy-equivalent scheme loads and resolves
    with payer=employee.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.social_contribution_scheme import (
    CollectionMechanism,
    ContributionPayer,
)
from saebooks.models.reference.withholding_table import FormulaType, WithholdingType

_AU_WITHHOLDING_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "withholding_tables.yaml"
)
_AU_CONTRIBUTION_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "social_contribution_schemes.yaml"
)


def test_au_withholding_table_seed_valid() -> None:
    doc = yaml.safe_load(_AU_WITHHOLDING_SEED.read_text())
    assert doc["table"] == "withholding_tables"
    rows = doc["rows"]
    assert rows, "AU withholding-tables seed is empty"

    keys = [(r["jurisdiction"], r["code"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, code, effective_from) in AU seed"

    withholding_types = {e.value for e in WithholdingType}
    formula_types = {e.value for e in FormulaType}
    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["withholding_type"] in withholding_types, (
            f"row {r['code']!r} has unknown withholding_type {r['withholding_type']!r}"
        )
        assert r["formula_type"] in formula_types, (
            f"row {r['code']!r} has unknown formula_type {r['formula_type']!r}"
        )
        assert isinstance(r["parameters"], dict) and r["parameters"], (
            f"row {r['code']!r} must have non-empty parameters"
        )

    by_code = {r["code"]: r for r in rows}
    assert by_code["au_payg_scale2"]["withholding_type"] == "wage_paye"
    assert by_code["au_payg_scale2"]["formula_type"] == "coefficient"


def test_au_social_contribution_scheme_seed_valid() -> None:
    doc = yaml.safe_load(_AU_CONTRIBUTION_SEED.read_text())
    assert doc["table"] == "social_contribution_schemes"
    rows = doc["rows"]
    assert rows, "AU social-contribution-schemes seed is empty"

    keys = [(r["jurisdiction"], r["code"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, code, effective_from) in AU seed"

    payers = {e.value for e in ContributionPayer}
    mechanisms = {e.value for e in CollectionMechanism}
    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["payer"] in payers, f"row {r['code']!r} has unknown payer {r['payer']!r}"
        assert r["collection_mechanism"] in mechanisms, (
            f"row {r['code']!r} has unknown collection_mechanism {r['collection_mechanism']!r}"
        )

    by_code = {r["code"]: r for r in rows}
    assert by_code["au_medicare"]["payer"] == "employee"
    assert by_code["au_medicare"]["collection_mechanism"] == "assessment"


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_withholding_tables() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t7")
    assert "AU/withholding_tables.yaml" in counts, (
        f"loader skipped the withholding-tables seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        row = (
            await s.execute(
                text(
                    "SELECT withholding_type, formula_type FROM withholding_tables "
                    "WHERE jurisdiction = 'AUS' AND code = 'au_payg_scale2'"
                )
            )
        ).one()
        assert row.withholding_type == "wage_paye"
        assert row.formula_type == "coefficient"


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_social_contribution_schemes() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t7")
    assert "AU/social_contribution_schemes.yaml" in counts, (
        f"loader skipped the social-contribution-schemes seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        payer = (
            await s.execute(
                text(
                    "SELECT payer FROM social_contribution_schemes "
                    "WHERE jurisdiction = 'AUS' AND code = 'au_medicare'"
                )
            )
        ).scalar_one()
        assert payer == "employee"
