"""M1.5 · T4 — entity-structure taxonomy: seed integrity + hierarchy load.

Two tests:
  * ``test_au_entity_structure_seed_buckets_valid`` — a pure-unit check
    (no DB) that every canonical_bucket in the AU seed is a real
    ``ENTITY_STRUCTURE_BUCKETS`` value and codes are unique. Runs in the
    standard suite and catches bucket typos before they reach a DB.
  * ``test_load_entity_structures`` — reference-DB integration (skipped
    unless the reference DB is configured, same gate as the loader test)
    proving the seed loads and a known local type resolves to its bucket.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.entity_structure import ENTITY_STRUCTURE_BUCKETS

_AU_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "entity_structure_types.yaml"
)


def test_au_entity_structure_seed_buckets_valid() -> None:
    doc = yaml.safe_load(_AU_SEED.read_text())
    assert doc["table"] == "entity_structure_types"
    rows = doc["rows"]
    assert rows, "AU entity-structure seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate entity-structure codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["canonical_bucket"] in ENTITY_STRUCTURE_BUCKETS, (
            f"row {r['code']!r} has unknown bucket {r['canonical_bucket']!r}"
        )

    # The structures Richard named must be representable and correctly bucketed.
    by_code = {r["code"]: r["canonical_bucket"] for r in rows}
    assert by_code["pty_ltd"] == "company_limited"
    assert by_code["disc_trust"] == "trust"
    assert by_code["smsf"] == "pension_fund"


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_entity_structures() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t4")
    assert "AU/entity_structure_types.yaml" in counts, (
        f"loader skipped the entity-structure seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        # smsf resolves to the pension_fund bucket.
        bucket = (
            await s.execute(
                text(
                    "SELECT canonical_bucket FROM entity_structure_types "
                    "WHERE jurisdiction = 'AUS' AND code = 'smsf'"
                )
            )
        ).scalar_one()
        assert bucket == "pension_fund"

        # Every seeded AU row maps to a known canonical bucket.
        bad = (
            await s.execute(
                text(
                    "SELECT count(*) FROM entity_structure_types "
                    "WHERE jurisdiction = 'AUS' AND canonical_bucket <> ALL(:buckets)"
                ),
                {"buckets": list(ENTITY_STRUCTURE_BUCKETS)},
            )
        ).scalar_one()
        assert bad == 0, f"{bad} AU rows have an unknown canonical_bucket"
