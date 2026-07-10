"""Data-transform correctness for migration 0186 (audit_snapshots backfill).

Migration 0186 is already applied to the shared test database schema by
the time these tests run (the harness migrates to head once, not per-test)
— there is nothing to "replay" via alembic without disrupting every other
test's schema state. Instead these tests exercise the SAME SQL the
migration's two backfill passes run, against synthetic rows created and
rolled back inside each test, proving the transform logic itself is
correct. Structural RLS assertions (relrowsecurity / policy presence /
cross-tenant probe / NULL-row fail-closed behaviour) live in
tests/services/test_rls_audit_snapshots.py.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.models.audit_snapshot import AuditSnapshot

pytestmark = pytest.mark.postgres_only


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


# Same transform logic as alembic/versions/0186_audit_snapshots_rls.py::
# upgrade()'s pass 1 (JSON-derived) and pass 2 (journal_lines self-join
# fallback) — with an added ``id = ANY(:ids)`` scoping guard so this test
# only ever touches the synthetic rows it creates (the real migration has
# no such guard; it runs once, unconditionally, over the whole table).
# The predicate/JOIN/regex are otherwise unchanged from the migration.
_BACKFILL_PASS1_SQL = """
    UPDATE audit_snapshots
    SET tenant_id = (COALESCE(before_data->>'tenant_id', after_data->>'tenant_id'))::uuid
    WHERE tenant_id IS NULL
      AND id = ANY(:ids)
      AND COALESCE(before_data->>'tenant_id', after_data->>'tenant_id')
          ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
"""

_BACKFILL_PASS2_SQL = """
    UPDATE audit_snapshots AS line_snap
    SET tenant_id = entry_snap.tenant_id
    FROM audit_snapshots AS entry_snap
    WHERE line_snap.tenant_id IS NULL
      AND line_snap.id = ANY(:ids)
      AND line_snap.table_name = 'journal_lines'
      AND entry_snap.table_name = 'journal_entries'
      AND entry_snap.tenant_id IS NOT NULL
      AND entry_snap.row_id = COALESCE(
            line_snap.before_data->>'entry_id',
            line_snap.after_data->>'entry_id'
          )
"""


async def test_0186_backfill_pass1_reads_tenant_id_from_json() -> None:
    if not _is_postgres():
        pytest.skip("migration SQL targets Postgres")
    tenant_id = DEFAULT_TENANT_ID
    row_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        snap = AuditSnapshot(
            table_name="accounts",
            row_id=str(row_id),
            action="update",
            before_data={"id": str(row_id), "tenant_id": str(tenant_id), "name": "old"},
            after_data={"id": str(row_id), "tenant_id": str(tenant_id), "name": "new"},
        )
        session.add(snap)
        await session.flush()
        snap_id = snap.id
        await session.execute(text(_BACKFILL_PASS1_SQL), {"ids": [snap_id]})
        result = await session.execute(
            text("SELECT tenant_id FROM audit_snapshots WHERE id = :id"), {"id": snap_id}
        )
        got = result.scalar_one()
        await session.rollback()
    assert got == tenant_id


async def test_0186_backfill_leaves_settings_rows_null() -> None:
    """The genuinely-underivable case: a settings-table snapshot (no
    tenant_id anywhere in its JSON) must stay NULL after both passes —
    never guessed."""
    if not _is_postgres():
        pytest.skip("migration SQL targets Postgres")
    async with AsyncSessionLocal() as session:
        snap = AuditSnapshot(
            table_name="settings",
            row_id="audit_mode",
            action="update",
            before_data={"key": "audit_mode", "value": "immutable"},
            after_data={"key": "audit_mode", "value": "immutable"},
        )
        session.add(snap)
        await session.flush()
        snap_id = snap.id
        await session.execute(text(_BACKFILL_PASS1_SQL), {"ids": [snap_id]})
        await session.execute(text(_BACKFILL_PASS2_SQL), {"ids": [snap_id]})
        result = await session.execute(
            text("SELECT tenant_id FROM audit_snapshots WHERE id = :id"), {"id": snap_id}
        )
        got = result.scalar_one()
        await session.rollback()
    assert got is None


async def test_0186_backfill_pass2_journal_lines_via_sibling_snapshot() -> None:
    """journal_lines has no tenant_id column — pass 2 must derive it via
    the sibling journal_entries snapshot written in the same cascade-
    delete transaction (see services/journal.py delete())."""
    if not _is_postgres():
        pytest.skip("migration SQL targets Postgres")
    tenant_id = DEFAULT_TENANT_ID
    entry_id = uuid.uuid4()
    line_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        entry_snap = AuditSnapshot(
            table_name="journal_entries",
            row_id=str(entry_id),
            action="delete",
            before_data={"id": str(entry_id), "tenant_id": str(tenant_id)},
        )
        line_snap = AuditSnapshot(
            table_name="journal_lines",
            row_id=str(line_id),
            action="delete",
            before_data={"id": str(line_id), "entry_id": str(entry_id)},
        )
        session.add_all([entry_snap, line_snap])
        await session.flush()
        entry_snap_id, line_snap_id = entry_snap.id, line_snap.id

        # Pass 1 first (resolves the entry_snap's own tenant_id from its
        # JSON), then pass 2 (self-join fallback for the line).
        await session.execute(text(_BACKFILL_PASS1_SQL), {"ids": [entry_snap_id, line_snap_id]})
        await session.execute(text(_BACKFILL_PASS2_SQL), {"ids": [entry_snap_id, line_snap_id]})

        result = await session.execute(
            text("SELECT tenant_id FROM audit_snapshots WHERE id = :id"), {"id": line_snap_id}
        )
        got = result.scalar_one()
        await session.rollback()
    assert got == tenant_id
