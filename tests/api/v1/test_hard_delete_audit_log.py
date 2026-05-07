"""Schema sanity tests for audit_log (gap ADMIN-DELETE-1)."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from saebooks.db import engine, AsyncSessionLocal


async def test_audit_log_table_exists() -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'audit_log'"
            )
        )
        assert result.scalar() == "audit_log"


async def test_audit_log_columns() -> None:
    """All required columns from the design doc are present."""
    expected = {
        "id",
        "tenant_id",
        "actor_user_id",
        "action",
        "table_name",
        "row_id",
        "row_snapshot",
        "reason",
        "at",
    }
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_log'"
            )
        )
        cols = {row[0] for row in result.fetchall()}
    missing = expected - cols
    assert not missing, f"audit_log missing columns: {missing}"


async def test_audit_log_indexes() -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_log'"
            )
        )
        idx = {row[0] for row in result.fetchall()}
    assert "ix_audit_log_tenant_at" in idx
    assert "ix_audit_log_table_row" in idx
