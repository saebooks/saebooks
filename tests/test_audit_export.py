"""Tests for audit.export_csv (service layer).

The legacy /admin/audit/export.csv HTML route was dropped in the Cat-C
rollup; an /api/v1 audit-log endpoint exists at /api/v1/admin/audit-log
but does not yet expose CSV export.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from saebooks.db import AsyncSessionLocal
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.services import audit as audit_svc


@pytest.fixture
async def scratch_snapshots() -> list[AuditSnapshot]:
    """Insert 3 known snapshots; remove on teardown."""
    tag = uuid.uuid4().hex[:8]
    ids: list[uuid.UUID] = []
    try:
        async with AsyncSessionLocal() as session:
            for i in range(3):
                snap = await audit_svc.snapshot(
                    session,
                    table_name=f"export_test_{tag}",
                    row_id=f"row-{i}",
                    action="update",
                    before_data={"value": f"before-{i}"},
                    after_data={"value": f"after-{i}"},
                    reason="pytest",
                    performed_by="pytest",
                )
                ids.append(snap.id)
            await session.commit()
        yield ids
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(AuditSnapshot).where(AuditSnapshot.id.in_(ids))
            )
            await session.commit()


async def test_export_csv_row_count_matches_snapshots(
    scratch_snapshots: list[uuid.UUID],
) -> None:
    async with AsyncSessionLocal() as session:
        # Pick a snapshot table_name filter so we only see ours
        tag_snap = await session.get(AuditSnapshot, scratch_snapshots[0])
        assert tag_snap is not None
        tbl = tag_snap.table_name
        csv_text = await audit_svc.export_csv(session, table_name=tbl)

    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == list(audit_svc.EXPORT_COLUMNS)
    assert len(rows) == 1 + 3  # header + 3 scratch snapshots


async def test_export_csv_fields_preserve_jsonb(
    scratch_snapshots: list[uuid.UUID],
) -> None:
    async with AsyncSessionLocal() as session:
        tag_snap = await session.get(AuditSnapshot, scratch_snapshots[0])
        assert tag_snap is not None
        tbl = tag_snap.table_name
        csv_text = await audit_svc.export_csv(session, table_name=tbl)

    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 3
    for r in rows:
        assert "before-" in r["before_data"]
        assert "after-" in r["after_data"]
        assert r["action"] == "update"
        assert r["performed_by"] == "pytest"


async def test_export_csv_date_window_filters_rows(
    scratch_snapshots: list[uuid.UUID],
) -> None:
    async with AsyncSessionLocal() as session:
        tag_snap = await session.get(AuditSnapshot, scratch_snapshots[0])
        assert tag_snap is not None
        tbl = tag_snap.table_name

        # Window that excludes everything — yesterday only
        yesterday = datetime.now(UTC) - timedelta(days=1)
        before_yesterday = yesterday - timedelta(days=1)
        csv_text = await audit_svc.export_csv(
            session,
            table_name=tbl,
            from_date=before_yesterday,
            to_date=yesterday,
        )
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert len(rows) == 1  # only the header


async def test_count_snapshots_matches_export(
    scratch_snapshots: list[uuid.UUID],
) -> None:
    async with AsyncSessionLocal() as session:
        tag_snap = await session.get(AuditSnapshot, scratch_snapshots[0])
        assert tag_snap is not None
        tbl = tag_snap.table_name
        count = await audit_svc.count_snapshots(session, table_name=tbl)
        csv_text = await audit_svc.export_csv(session, table_name=tbl)
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert count == len(rows) - 1  # header excluded from count


# NOTE: HTML route tests for /admin/audit/export.csv removed; replace with
# tests against /api/v1/admin/audit-log when CSV export is ported.
