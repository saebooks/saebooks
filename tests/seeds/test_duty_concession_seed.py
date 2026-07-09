"""M1.5 · T5 — duty concession taxonomy: seed integrity + reference-DB load.

Two tests, mirroring ``tests/seeds/test_entity_structure_seed.py``:
  * ``test_au_duty_concession_seed_relief_types_valid`` — a pure-unit
    check (no DB) that every ``relief_type`` in the AU seed is a real
    ``DUTY_RELIEF_TYPES`` value and codes are unique. Runs in the
    standard suite and catches a typo before it reaches a DB.
  * ``test_load_duty_concessions`` — reference-DB integration (skipped
    unless the reference DB is configured, same gate as the loader test)
    proving the seed loads and a known concession resolves.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.duty_concession import DUTY_RELIEF_TYPES

_AU_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "duty_concessions.yaml"
)


def test_au_duty_concession_seed_relief_types_valid() -> None:
    doc = yaml.safe_load(_AU_SEED.read_text())
    assert doc["table"] == "duty_concessions"
    rows = doc["rows"]
    assert rows, "AU duty-concession seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate duty-concession codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["relief_type"] in DUTY_RELIEF_TYPES, (
            f"row {r['code']!r} has unknown relief_type {r['relief_type']!r}"
        )

    by_code = {r["code"]: r["relief_type"] for r in rows}
    assert by_code["first_home_concession"] == "threshold_abatement"
    assert by_code["family_farm_exemption"] == "full_exemption"


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_duty_concessions() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t5")
    assert "AU/duty_concessions.yaml" in counts, (
        f"loader skipped the duty-concession seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        relief_type = (
            await s.execute(
                text(
                    "SELECT relief_type FROM duty_concessions "
                    "WHERE jurisdiction = 'AUS' AND code = 'first_home_concession'"
                )
            )
        ).scalar_one()
        assert relief_type == "threshold_abatement"

        # Every seeded AU row maps to a known relief_type.
        bad = (
            await s.execute(
                text(
                    "SELECT count(*) FROM duty_concessions "
                    "WHERE jurisdiction = 'AUS' AND relief_type <> ALL(:types)"
                ),
                {"types": list(DUTY_RELIEF_TYPES)},
            )
        ).scalar_one()
        assert bad == 0, f"{bad} AU rows have an unknown relief_type"
