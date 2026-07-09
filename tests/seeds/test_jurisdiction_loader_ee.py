"""End-to-end: clean reference DB -> load EE -> verify row counts.

Mirrors tests/seeds/test_jurisdiction_loader.py's AU shape. Proves the
EE seed set (all 14 files) loads through the real loader, is idempotent,
and that the directory-name (EE) -> reference-code (EST) resolution the
task's TRAP asked for actually round-trips through a live database.
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
async def test_load_ee_idempotent() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    counts1 = await load_seeds("EE", version_tag="test-ee-1")
    expected_files = {
        "_global/jurisdictions.yaml",
        "_global/currencies.yaml",
        "_global/countries.yaml",
        "EE/entity_structure_types.yaml",
        "EE/tax_codes.yaml",
        "EE/tax_return_box_definitions.yaml",
        "EE/withholding_tables.yaml",
        "EE/social_contribution_schemes.yaml",
        "EE/mandatory_contribution_rules.yaml",
        "EE/corporate_tax_rates.yaml",
        "EE/dividend_relief_mechanisms.yaml",
        "EE/capital_gains_tax_regimes.yaml",
        "EE/benefit_in_kind_rates.yaml",
        "EE/retirement_vehicle_types.yaml",
        "EE/duty_concessions.yaml",
        "EE/chart_template.yaml",
        "EE/fiscal_year_definitions.yaml",
    }
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected EE seed files: missing={expected_files - set(counts1)}"
    )

    # Second load — idempotent no-op.
    counts2 = await load_seeds("EE", version_tag="test-ee-2")
    assert counts1 == counts2, "Idempotent re-run should report identical row counts"

    async with ReferenceMigrationSession() as s:
        # The directory-name (EE) -> reference-code (EST) TRAP: every row
        # must land under jurisdiction='EST', not 'EE'.
        n_tax_codes = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        # 22, not 18: INPUT_EXEMPT was added (purchase-
        # direction VAT-exempt code — chart_template.yaml's Bank Fees
        # account previously reused the sale-direction EXEMPT code, which
        # has the wrong direction for a purchase-bucket account). Then
        # dated legacy-rate rows were added (20% until
        # 2023-12-31, 22% 2024-01-01-2025-06-30) for RC_EU_ACQ and
        # INPUT_CAP, matching the STD/INPUT_STD dated-history pattern
        # (+4 rows: 18 -> 22).
        assert n_tax_codes == 22, f"Expected 22 EE tax codes, got {n_tax_codes}"

        n_entities = (
            await s.execute(
                text("SELECT count(*) FROM entity_structure_types WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        assert n_entities == 8, f"Expected 8 EE entity structure types, got {n_entities}"

        n_boxes = (
            await s.execute(
                text("SELECT count(*) FROM tax_return_box_definitions WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        assert n_boxes == 40, f"Expected 40 EE tax-return box definitions, got {n_boxes}"

        return_types = {
            r[0]
            for r in (
                await s.execute(
                    text(
                        "SELECT DISTINCT return_type FROM tax_return_box_definitions "
                        "WHERE jurisdiction = 'EST'"
                    )
                )
            ).all()
        }
        assert return_types == {"KMD", "KMD-INF", "TSD", "OSS-Q"}, return_types

        # jurisdiction='EE' (the directory name, not the reference code)
        # must NOT have leaked into any row.
        n_wrong_code = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'EE'")
            )
        ).scalar_one()
        assert n_wrong_code == 0, "jurisdiction='EE' leaked in — must be 'EST'"

        # Distributed-profit 22/78 gross-up row + paired 0% retained row.
        rates = {
            r.entity_scope: float(r.rate_percent)
            for r in (
                await s.execute(
                    text(
                        "SELECT entity_scope, rate_percent FROM corporate_tax_rates "
                        "WHERE jurisdiction = 'EST' AND tax_year = 2026"
                    )
                )
            ).all()
        }
        assert rates["retained_reinvested"] == 0.0
        assert rates["distributed_profit"] == 22.0

        # schema_meta reflects the second tag.
        tag = (
            await s.execute(text("SELECT version_tag FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        assert tag == "test-ee-2"
