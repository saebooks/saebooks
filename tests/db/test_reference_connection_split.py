"""Connection-role separation: app role can read reference, can't write.

Skipped unless both REFERENCE_DATABASE_URL and
REFERENCE_MIGRATION_DATABASE_URL are set in the test environment.

These tests assert the *behavioural* split that the architecture
relies on. If the app role somehow gains write privileges, this test
fails immediately rather than silently letting runtime code mutate
reference data.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("REFERENCE_DATABASE_URL")
        and os.environ.get("REFERENCE_MIGRATION_DATABASE_URL")
    ),
    reason="Reference DB env vars not configured",
)


@pytest.mark.asyncio
async def test_app_role_read_reference_succeeds() -> None:
    from saebooks.db import ReferenceSession

    assert ReferenceSession is not None
    async with ReferenceSession() as s:
        # Even on a freshly-migrated DB this returns 0 — we just need
        # the SELECT to not blow up under default_transaction_read_only.
        rs = await s.execute(text("SELECT count(*) FROM jurisdictions"))
        assert rs.scalar_one() >= 0


@pytest.mark.asyncio
async def test_app_role_write_reference_fails() -> None:
    """Writes via the app role must be refused at the transaction level."""
    from saebooks.db import ReferenceSession

    assert ReferenceSession is not None
    async with ReferenceSession() as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                text(
                    "INSERT INTO jurisdictions (code, name, currency_default) "
                    "VALUES (:code, 'X', 'USD')"
                ).bindparams(code="ZZZ")
            )
            await s.commit()


@pytest.mark.asyncio
async def test_migration_role_can_write_reference() -> None:
    from saebooks.db import ReferenceMigrationSession

    assert ReferenceMigrationSession is not None
    sentinel = f"T{uuid.uuid4().hex[:2].upper()}"  # 3-char ISO-ish placeholder
    async with ReferenceMigrationSession() as s:
        await s.execute(
            text(
                "INSERT INTO jurisdictions (code, name, currency_default, active) "
                "VALUES (:c, 'sentinel', 'USD', false) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(c=sentinel)
        )
        await s.commit()

        # Round-trip
        rs = await s.execute(
            text("SELECT name FROM jurisdictions WHERE code = :c").bindparams(
                c=sentinel
            )
        )
        assert rs.scalar_one() == "sentinel"

        # Cleanup
        await s.execute(
            text("DELETE FROM jurisdictions WHERE code = :c").bindparams(
                c=sentinel
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_company_session_independent_of_reference() -> None:
    """A failure on one engine must not poison the other."""
    from saebooks.db import AsyncSessionLocal, ReferenceSession

    assert ReferenceSession is not None
    async with ReferenceSession() as ref:
        await ref.execute(text("SELECT 1"))
    async with AsyncSessionLocal() as co:
        await co.execute(text("SELECT 1"))
