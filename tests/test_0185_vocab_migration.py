"""Data-transform correctness for migration 0185 (extended_audit_modes vocab).

Migration 0185 is already applied to the shared test database schema by
the time this test runs (the harness migrates to head once, not per-test)
— there is nothing to "replay" via alembic without disrupting every other
test's schema state. Instead this test exercises the SAME CASE SQL the
migration runs, against a synthetic row created and rolled back inside
the test, proving the transform logic itself is correct.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


# Same CASE transform as alembic/versions/0185_audit_mode_vocab.py::
# upgrade() — with an added ``WHERE id = :cid`` so this test only ever
# touches the one synthetic row it creates (the real migration has no
# such guard; it runs once, unconditionally, over every company).
_VOCAB_CASE_SQL = """
    UPDATE companies
    SET audit_mode = CASE audit_mode
        WHEN 'mutable' THEN 'open'
        WHEN 'draft'   THEN 'hybrid'
        WHEN 'immutable' THEN 'immutable'
        WHEN 'open'    THEN 'open'
        WHEN 'hybrid'  THEN 'hybrid'
        ELSE 'immutable'
    END
    WHERE id = :cid
"""


@pytest.mark.parametrize(
    "legacy_value,expected",
    [
        ("immutable", "immutable"),
        ("mutable", "open"),
        ("draft", "hybrid"),
        ("open", "open"),  # idempotent — already-new-vocab rows pass through
        ("hybrid", "hybrid"),
        ("something-unexpected", "immutable"),  # fail-safe default
    ],
)
async def test_0185_vocab_case_mapping(legacy_value: str, expected: str) -> None:
    if not _is_postgres():
        pytest.skip("migration SQL targets Postgres")
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(id=cid, tenant_id=DEFAULT_TENANT_ID, name=f"vocab-{cid.hex[:6]}")
        )
        await session.flush()
        # Bypass the (now-restricted) ORM validator to seed a legacy value.
        await session.execute(
            Company.__table__.update().where(Company.id == cid).values(audit_mode=legacy_value)
        )
        await session.execute(text(_VOCAB_CASE_SQL), {"cid": cid})
        result = await session.execute(
            text("SELECT audit_mode FROM companies WHERE id = :cid"), {"cid": cid}
        )
        got = result.scalar_one()
        await session.rollback()  # never persist synthetic test data
    assert got == expected
