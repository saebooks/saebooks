"""End-to-end: clean reference DB → load AU → verify tax_codes count."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytest.mark.asyncio
async def test_load_au_idempotent() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    # First load — should populate.
    counts1 = await load_seeds("AU", version_tag="test-au-1")
    # Files we expect to have been touched: at minimum the global
    # registries (jurisdictions/currencies/countries) plus the AU
    # tax_codes / chart_template / fiscal_year_definitions.
    expected_files = {
        "_global/jurisdictions.yaml",
        "_global/currencies.yaml",
        "_global/countries.yaml",
        "AU/tax_codes.yaml",
        "AU/chart_template.yaml",
        "AU/fiscal_year_definitions.yaml",
    }
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected seed files: missing={expected_files - set(counts1)}"
    )

    # Second load — should be a no-op (UPSERTs all match existing rows).
    counts2 = await load_seeds("AU", version_tag="test-au-2")
    assert counts1 == counts2, (
        "Idempotent re-run should report identical row counts"
    )

    # Verify count (12 codes in the seed file at v0.1.4).
    async with ReferenceMigrationSession() as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'AUS'")
            )
        ).scalar_one()
        assert n == 12, f"Expected 12 AU tax codes seeded, got {n}"

        # schema_meta should now read the second tag.
        tag = (
            await s.execute(text("SELECT version_tag FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        assert tag == "test-au-2"
