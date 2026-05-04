"""Cat-C admin contract tests for /api/v1/admin/audit-log + sql/execute.

Covers (matching the W5 brief):

* audit log: returns paginated rows, filters work, tenant-scoped
* sql/execute: SELECT runs as ``saebooks_sql_ro``, returns rows,
  audit row written
* sql/execute: UPDATE without write_confirmation → 403, audit row says
  status=rejected
* sql/execute: UPDATE with matching write_confirmation → runs as
  ``saebooks_app``, audit row says role_used=saebooks_app
* sql/execute: UPDATE with mismatched write_confirmation verb → 403,
  audit row written with status=rejected
* FLAG_SQL_TOOL gate: community / business tier rejected on
  /sql/execute (404 — feature gate semantics in this codebase)
"""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.change_log import ChangeLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_api_client() -> AsyncClient:
    """Bearer client with X-Admin: true (admin gate satisfied via header
    fallback because the static dev bearer has no JWT sub)."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Admin": "true",
        },
    ) as ac:
        yield ac


@pytest.fixture
async def plain_api_client() -> AsyncClient:
    """Bearer client without X-Admin — 403 path."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def _force_pro_edition(monkeypatch):
    """Pin edition to ``pro`` for these tests — FLAG_SQL_TOOL is Pro+.

    The flag-gate test below overrides this back to ``community`` to
    verify the negative path. We touch ``services.features`` config
    so ``is_enabled(FLAG_SQL_TOOL)`` returns the correct value during
    the request.
    """
    from saebooks.config import settings as _settings

    original = _settings.edition
    _settings.edition = "pro"
    yield
    _settings.edition = original


@pytest.fixture
async def disposable_table() -> str:
    """Create a small per-test scratch table, return its name, drop on teardown.

    Used by the UPDATE/DELETE confirmation tests — we don't want to
    rummage in real data tables (``contacts`` is RLS-bound and the
    test has no easy hook to seed cross-tenant rows for the saebooks_app
    role to mutate). A bespoke unlogged table sidesteps both concerns.
    """
    name = f"sql_tool_test_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text as sa_text

        await session.execute(
            sa_text(
                f"CREATE UNLOGGED TABLE {name} ("
                "  id INT PRIMARY KEY,"
                "  label TEXT NOT NULL"
                ")"
            )
        )
        # Grant DML to saebooks_app (and SELECT to saebooks_sql_ro via
        # pg_read_all_data — that's automatic).
        await session.execute(sa_text(f"GRANT ALL ON TABLE {name} TO saebooks_app"))
        await session.execute(
            sa_text(f"INSERT INTO {name} (id, label) VALUES (1, 'one'), (2, 'two')")
        )
        await session.commit()
    yield name
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text as sa_text

        await session.execute(sa_text(f"DROP TABLE IF EXISTS {name}"))
        await session.commit()


async def _latest_sql_audit_row() -> ChangeLog | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChangeLog)
                .where(ChangeLog.entity == "sql_tool")
                .order_by(ChangeLog.id.desc())
                .limit(1)
            )
        ).scalars().first()
    return row


# ---------------------------------------------------------------------------
# Auth / admin gate
# ---------------------------------------------------------------------------


async def test_audit_log_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/admin/audit-log")
    assert r.status_code == 401


async def test_audit_log_requires_admin(plain_api_client: AsyncClient) -> None:
    r = await plain_api_client.get("/api/v1/admin/audit-log")
    assert r.status_code == 403


async def test_sql_execute_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.post(
        "/api/v1/admin/sql/execute", json={"statement": "SELECT 1"}
    )
    assert r.status_code == 401


async def test_sql_execute_requires_admin(plain_api_client: AsyncClient) -> None:
    r = await plain_api_client.post(
        "/api/v1/admin/sql/execute", json={"statement": "SELECT 1"}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Audit log — pagination + filters
# ---------------------------------------------------------------------------


async def test_audit_log_returns_paginated_rows(
    admin_api_client: AsyncClient,
) -> None:
    # Run a SELECT to guarantee at least one sql_tool audit row exists.
    await admin_api_client.post(
        "/api/v1/admin/sql/execute", json={"statement": "SELECT 1 AS one"}
    )
    r = await admin_api_client.get(
        "/api/v1/admin/audit-log", params={"limit": 5, "offset": 0}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert data["limit"] == 5
    assert data["offset"] == 0
    assert isinstance(data["items"], list)
    # The latest one should be our SELECT.
    sql_items = [i for i in data["items"] if i["entity"] == "sql_tool"]
    assert sql_items, "expected at least one sql_tool audit entry"


async def test_audit_log_filter_route(admin_api_client: AsyncClient) -> None:
    await admin_api_client.post(
        "/api/v1/admin/sql/execute", json={"statement": "SELECT 42"}
    )
    r = await admin_api_client.get(
        "/api/v1/admin/audit-log", params={"route": "sql_tool", "limit": 10}
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i["entity"] == "sql_tool" for i in items)
    assert items, "filter on sql_tool should return at least one row"


async def test_audit_log_filter_status(admin_api_client: AsyncClient) -> None:
    # Cause a rejection to seed a status=rejected row.
    await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={"statement": "DELETE FROM contacts WHERE 1=0"},
    )
    r = await admin_api_client.get(
        "/api/v1/admin/audit-log", params={"status": "rejected", "limit": 10}
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "expected at least one rejected audit row"
    assert all(i["payload"].get("status") == "rejected" for i in items)


# ---------------------------------------------------------------------------
# /sql/execute — SELECT
# ---------------------------------------------------------------------------


async def test_sql_execute_select_runs_as_ro(
    admin_api_client: AsyncClient,
) -> None:
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={"statement": "SELECT 1 AS one, 'a' AS letter"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role_used"] == "saebooks_sql_ro"
    assert body["columns"] == ["one", "letter"]
    assert body["rows"] == [[1, "a"]]
    assert body["audit_id"] > 0
    # Audit row matches.
    row = await _latest_sql_audit_row()
    assert row is not None
    assert row.id == body["audit_id"]
    assert row.payload["role_used"] == "saebooks_sql_ro"
    assert row.payload["status"] == "ok"


# ---------------------------------------------------------------------------
# /sql/execute — write rejection without confirmation
# ---------------------------------------------------------------------------


async def test_sql_execute_update_without_confirmation_rejected(
    admin_api_client: AsyncClient, disposable_table: str
) -> None:
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={"statement": f"UPDATE {disposable_table} SET label = 'x'"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == "write_rejected"
    assert "audit_id" in body
    row = await _latest_sql_audit_row()
    assert row is not None
    assert row.id == body["audit_id"]
    assert row.payload["status"] == "rejected"


async def test_sql_execute_update_with_disabled_confirmation_rejected(
    admin_api_client: AsyncClient, disposable_table: str
) -> None:
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={
            "statement": f"UPDATE {disposable_table} SET label = 'x'",
            "write_confirmation": {"enabled": False, "verb_typed": "UPDATE"},
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /sql/execute — write succeeds with matching confirmation
# ---------------------------------------------------------------------------


async def test_sql_execute_update_with_matching_confirmation_runs_as_app(
    admin_api_client: AsyncClient, disposable_table: str
) -> None:
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={
            "statement": f"UPDATE {disposable_table} SET label = 'updated' WHERE id = 1",
            "write_confirmation": {"enabled": True, "verb_typed": "UPDATE"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role_used"] == "saebooks_app"
    assert body["rowcount"] == 1
    row = await _latest_sql_audit_row()
    assert row is not None
    assert row.id == body["audit_id"]
    assert row.payload["role_used"] == "saebooks_app"
    assert row.payload["status"] == "ok"


async def test_sql_execute_lowercase_confirmation_verb_runs_as_app(
    admin_api_client: AsyncClient, disposable_table: str
) -> None:
    """Confirmation verb is case-insensitive (brief: ``upper().strip()``)."""
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={
            "statement": f"update {disposable_table} SET label = 'lc'",
            "write_confirmation": {"enabled": True, "verb_typed": " update "},
        },
    )
    assert r.status_code == 200
    assert r.json()["role_used"] == "saebooks_app"


# ---------------------------------------------------------------------------
# /sql/execute — write rejected on verb mismatch
# ---------------------------------------------------------------------------


async def test_sql_execute_update_with_delete_verb_rejected(
    admin_api_client: AsyncClient, disposable_table: str
) -> None:
    r = await admin_api_client.post(
        "/api/v1/admin/sql/execute",
        json={
            "statement": f"UPDATE {disposable_table} SET label = 'mismatched'",
            "write_confirmation": {"enabled": True, "verb_typed": "DELETE"},
        },
    )
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "write_rejected"
    row = await _latest_sql_audit_row()
    assert row is not None
    assert row.id == body["audit_id"]
    assert row.payload["status"] == "rejected"
    assert "mismatch" in (row.payload.get("error") or "").lower()


# ---------------------------------------------------------------------------
# FLAG_SQL_TOOL gate — community/business rejected
# ---------------------------------------------------------------------------


async def test_sql_execute_blocked_on_community_edition(
    admin_api_client: AsyncClient,
) -> None:
    """FLAG_SQL_TOOL is Pro+; community returns 404 (require_feature)."""
    from saebooks.config import settings as _settings

    saved = _settings.edition
    _settings.edition = "community"
    try:
        r = await admin_api_client.post(
            "/api/v1/admin/sql/execute", json={"statement": "SELECT 1"}
        )
    finally:
        _settings.edition = saved
    assert r.status_code == 404


async def test_sql_execute_blocked_on_business_edition(
    admin_api_client: AsyncClient,
) -> None:
    from saebooks.config import settings as _settings

    saved = _settings.edition
    _settings.edition = "business"
    try:
        r = await admin_api_client.post(
            "/api/v1/admin/sql/execute", json={"statement": "SELECT 1"}
        )
    finally:
        _settings.edition = saved
    assert r.status_code == 404
