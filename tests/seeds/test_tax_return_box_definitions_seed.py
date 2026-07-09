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
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.services.tax_return_generator import _BoxDefRow, _parse_box_definition

_AU_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
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
