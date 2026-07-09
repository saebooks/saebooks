"""M1.5 · T6 — generic retirement-vehicle + mandatory-contribution seed
integrity + reference-DB load.

Four tests:
  * ``test_au_retirement_vehicle_type_seed_valid`` — pure-unit check
    (no DB) that the AU retirement-vehicle-types seed has valid enum
    values and unique keys.
  * ``test_au_mandatory_contribution_rule_seed_valid`` — pure-unit check
    (no DB) that the AU mandatory-contribution-rules seed has valid enum
    values and unique keys.
  * ``test_load_retirement_vehicle_types`` — reference-DB integration
    (skipped unless the reference DB is configured, same gate as the
    loader test) proving the AU super-fund vehicle types load and resolve.
  * ``test_load_mandatory_contribution_rules`` — reference-DB integration
    proving the AU Superannuation Guarantee rule loads and resolves with
    payer=employer.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.mandatory_contribution_rule import (
    MandatoryContributionPayer,
)
from saebooks.models.reference.retirement_vehicle import (
    RETIREMENT_TAX_TREATMENTS,
    RETIREMENT_VEHICLE_BUCKETS,
)

_AU_VEHICLE_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "retirement_vehicle_types.yaml"
)
_AU_CONTRIBUTION_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "mandatory_contribution_rules.yaml"
)


def test_au_retirement_vehicle_type_seed_valid() -> None:
    doc = yaml.safe_load(_AU_VEHICLE_SEED.read_text())
    assert doc["table"] == "retirement_vehicle_types"
    rows = doc["rows"]
    assert rows, "AU retirement-vehicle-types seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate retirement-vehicle codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["canonical_bucket"] in RETIREMENT_VEHICLE_BUCKETS, (
            f"row {r['code']!r} has unknown bucket {r['canonical_bucket']!r}"
        )
        assert r["tax_treatment"] in RETIREMENT_TAX_TREATMENTS, (
            f"row {r['code']!r} has unknown tax_treatment {r['tax_treatment']!r}"
        )

    # The vehicles Richard named must be representable and correctly bucketed.
    by_code = {r["code"]: r for r in rows}
    assert by_code["apra_super"]["canonical_bucket"] == "occupational_pension"
    assert by_code["apra_super"]["tax_treatment"] == "EET"
    assert by_code["smsf"]["canonical_bucket"] == "self_directed"


def test_au_mandatory_contribution_rule_seed_valid() -> None:
    doc = yaml.safe_load(_AU_CONTRIBUTION_SEED.read_text())
    assert doc["table"] == "mandatory_contribution_rules"
    rows = doc["rows"]
    assert rows, "AU mandatory-contribution-rules seed is empty"

    keys = [(r["jurisdiction"], r["code"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), (
        "duplicate (jurisdiction, code, effective_from) in AU seed"
    )

    payers = {e.value for e in MandatoryContributionPayer}
    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["payer"] in payers, f"row {r['code']!r} has unknown payer {r['payer']!r}"
        assert r["rate_percent"] > 0, f"row {r['code']!r} must have a positive rate"

    # au_super_guarantee is a dated series (11.5% FY24-25, then the legislated
    # 12% from 2025-07-01) — key by effective_from, not code alone.
    sg_rows = {
        r["effective_from"]: r
        for r in rows
        if r["code"] == "au_super_guarantee"
    }
    assert sg_rows, "expected au_super_guarantee rows"
    for r in sg_rows.values():
        assert r["payer"] == "employer"
        assert r["earnings_base"] == "ordinary_time_earnings"
    rates = {float(r["rate_percent"]) for r in sg_rows.values()}
    assert 11.5 in rates and 12.0 in rates, (
        f"expected the 11.5% and legislated 12% SG rows; got {rates}"
    )


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_retirement_vehicle_types() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t6")
    assert "AU/retirement_vehicle_types.yaml" in counts, (
        f"loader skipped the retirement-vehicle-types seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        row = (
            await s.execute(
                text(
                    "SELECT canonical_bucket, tax_treatment FROM retirement_vehicle_types "
                    "WHERE jurisdiction = 'AUS' AND code = 'smsf'"
                )
            )
        ).one()
        assert row.canonical_bucket == "self_directed"
        assert row.tax_treatment == "EET"

        # Every seeded AU row maps to a known canonical bucket.
        bad = (
            await s.execute(
                text(
                    "SELECT count(*) FROM retirement_vehicle_types "
                    "WHERE jurisdiction = 'AUS' AND canonical_bucket <> ALL(:buckets)"
                ),
                {"buckets": list(RETIREMENT_VEHICLE_BUCKETS)},
            )
        ).scalar_one()
        assert bad == 0, f"{bad} AU rows have an unknown canonical_bucket"


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_mandatory_contribution_rules() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t6")
    assert "AU/mandatory_contribution_rules.yaml" in counts, (
        f"loader skipped the mandatory-contribution-rules seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        # Dated series (11.5% FY24-25 then 12% from 2025-07-01) — pin the row.
        row = (
            await s.execute(
                text(
                    "SELECT payer, rate_percent, earnings_base FROM mandatory_contribution_rules "
                    "WHERE jurisdiction = 'AUS' AND code = 'au_super_guarantee' "
                    "AND effective_from = '2024-07-01'"
                )
            )
        ).one()
        assert row.payer == "employer"
        assert row.earnings_base == "ordinary_time_earnings"
        assert float(row.rate_percent) == pytest.approx(11.5)
        # The legislated 12% row is present from 2025-07-01.
        rate_2025 = (
            await s.execute(
                text(
                    "SELECT rate_percent FROM mandatory_contribution_rules "
                    "WHERE jurisdiction = 'AUS' AND code = 'au_super_guarantee' "
                    "AND effective_from = '2025-07-01'"
                )
            )
        ).scalar_one()
        assert float(rate_2025) == pytest.approx(12.0)
