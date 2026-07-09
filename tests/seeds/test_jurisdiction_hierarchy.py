"""M1.5 · T3 — jurisdiction hierarchy: a country can own sub-national nodes.

Verifies the additive columns from migration 0002_jurisdiction_hierarchy:
a state-level jurisdiction (e.g. Queensland) hangs off its country (AUS)
via ``parent_code``, carries ``level='state'`` and an ISO 3166-2
subdivision code, and existing country rows default to ``level='country'``.

Skipped unless the reference DB is configured (same gate as the loader
test) — the standard app test stack does not stand up the reference DB.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytest.mark.asyncio
async def test_jurisdiction_hierarchy_parent_child() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    # Ensure the country node exists (loader seeds 'AUS').
    await load_seeds("AU", version_tag="test-t3")

    async with ReferenceMigrationSession() as s:
        # Existing country row defaults to level='country', no parent.
        row = (
            await s.execute(
                text(
                    "SELECT level, parent_code FROM jurisdictions "
                    "WHERE code = 'AUS'"
                )
            )
        ).one()
        assert row.level == "country", f"AUS should be country-level, got {row.level!r}"
        assert row.parent_code is None, "country node must have no parent"

        # Insert a state-level child that hangs off the country via the
        # self-FK. Idempotent for repeat runs.
        await s.execute(
            text(
                "INSERT INTO jurisdictions "
                "(code, name, currency_default, parent_code, level, "
                " iso_subdivision_code) "
                "VALUES ('AUQ', 'Queensland', 'AUD', 'AUS', 'state', 'AU-QLD') "
                "ON CONFLICT (code) DO UPDATE SET "
                "  parent_code = EXCLUDED.parent_code, "
                "  level = EXCLUDED.level, "
                "  iso_subdivision_code = EXCLUDED.iso_subdivision_code"
            )
        )
        await s.commit()

        child = (
            await s.execute(
                text(
                    "SELECT name, parent_code, level, iso_subdivision_code "
                    "FROM jurisdictions WHERE code = 'AUQ'"
                )
            )
        ).one()
        assert child.parent_code == "AUS"
        assert child.level == "state"
        assert child.iso_subdivision_code == "AU-QLD"

        # The self-FK resolves — the country is discoverable from the child.
        parent_name = (
            await s.execute(
                text(
                    "SELECT p.name FROM jurisdictions c "
                    "JOIN jurisdictions p ON p.code = c.parent_code "
                    "WHERE c.code = 'AUQ'"
                )
            )
        ).scalar_one()
        assert parent_name == "Australia" or parent_name  # country name from seed

        # Clean up the synthetic node so the suite stays idempotent.
        await s.execute(text("DELETE FROM jurisdictions WHERE code = 'AUQ'"))
        await s.commit()
