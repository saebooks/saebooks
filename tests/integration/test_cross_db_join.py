"""Cross-DB lookup: write a tax_period in company DB referencing a
reference-DB tax_code, then resolve via two sessions.

The point is to exercise the architectural rule: joins do NOT cross
the database boundary; instead the service layer makes a second query
against the other engine.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("REFERENCE_DATABASE_URL")
        and os.environ.get("REFERENCE_MIGRATION_DATABASE_URL")
    ),
    reason="Reference DB env vars not configured",
)


@pytest.mark.asyncio
async def test_app_level_lookup_resolves_tax_code() -> None:
    """Insert a TaxPeriod against AU; look up the AU GST code from
    reference; assert both sessions can be used in one logical
    operation without a SQL-level join.
    """
    from saebooks.db import AsyncSessionLocal, ReferenceMigrationSession, ReferenceSession
    from saebooks.models.company import Company
    from saebooks.models.reference.tax_code import RefTaxCode
    from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
    from saebooks.services.reference.loader import load_seeds

    # Make sure the seed data is present (idempotent).
    await load_seeds("AU", version_tag="cross-db-test")

    # Find any company with jurisdiction AU. The seed company should
    # work since the migration backfilled jurisdiction='AU'.
    async with AsyncSessionLocal() as s:
        co = (
            await s.execute(select(Company).where(Company.jurisdiction == "AU").limit(1))
        ).scalars().first()
        assert co is not None, (
            "Expected at least one company with jurisdiction='AU' in the company DB"
        )

        period = TaxPeriod(
            id=uuid.uuid4(),
            company_id=co.id,
            tenant_id=co.tenant_id,
            jurisdiction="AUS",
            period_type=TaxPeriodType.QUARTERLY,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
        )
        s.add(period)
        try:
            await s.commit()
        except Exception:
            await s.rollback()
            # Race with a previous run that left the same period — just
            # find it again.
            existing = (
                await s.execute(
                    select(TaxPeriod).where(
                        TaxPeriod.company_id == co.id,
                        TaxPeriod.jurisdiction == "AUS",
                        TaxPeriod.period_start == date(2026, 7, 1),
                    )
                )
            ).scalars().first()
            assert existing is not None
            period = existing

    # Now resolve the tax code referenced by this period's likely
    # feeders, against the reference DB.
    assert ReferenceSession is not None
    async with ReferenceSession() as ref:
        gst = (
            await ref.execute(
                select(RefTaxCode).where(
                    RefTaxCode.jurisdiction == "AUS",
                    RefTaxCode.code == "GST",
                )
            )
        ).scalars().first()
        assert gst is not None, "AU GST code missing from reference DB"
        assert float(gst.rate_percent) == 10.0
        assert gst.direction.value == "sale"

    # Sanity: the company-DB session did NOT pollute the reference DB.
    async with ReferenceMigrationSession() as ref:  # type: ignore[union-attr]
        n = (
            await ref.execute(
                text("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_name = 'tax_periods'")
            )
        ).scalar_one()
        assert n == 0, "tax_periods leaked into reference DB"
