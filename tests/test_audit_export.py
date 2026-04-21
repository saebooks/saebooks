"""Tests for audit.export_csv + /admin/audit/export.csv."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.user import User
from saebooks.services import audit as audit_svc


async def _cleanup_user(username: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.username == username))
        await session.commit()


@pytest.fixture
async def unique_username() -> str:
    name = f"audit-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        await _cleanup_user(name)


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


async def test_audit_export_route_requires_accountant(
    client: AsyncClient, unique_username: str
) -> None:
    """readonly default role gets 403."""
    r = await client.get(
        "/admin/audit/export.csv",
        headers={"Remote-User": unique_username},
    )
    assert r.status_code == 403


async def test_audit_export_route_returns_csv(
    client: AsyncClient,
    unique_username: str,
    scratch_snapshots: list[uuid.UUID],
) -> None:
    # Promote to accountant
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        u.role = "accountant"
        await session.commit()

        tag_snap = await session.get(AuditSnapshot, scratch_snapshots[0])
        assert tag_snap is not None
        tbl = tag_snap.table_name

    r = await client.get(
        f"/admin/audit/export.csv?table_name={tbl}",
        headers={"Remote-User": unique_username},
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(audit_svc.EXPORT_COLUMNS)
    assert len(rows) == 1 + 3
