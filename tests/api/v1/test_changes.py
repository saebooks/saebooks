"""Contract tests for /api/v1/changes.

Gap noted in the 2026-05-23 overnight regression sweep — the offline-sync
change-log feed has unit-level test coverage on the service but ZERO
contract coverage on the public NDJSON streaming endpoint, even though
that's the surface offline desktop clients rely on.

Covers:
* Auth gate (401 without bearer).
* Empty feed — 200 NDJSON with X-Cursor-Next echoing the input cursor.
* Populated feed — line count, X-Row-Count header, X-Cursor-Next == last id.
* ``since`` query param — cursor advances only past the supplied id.
* ``limit`` clamp — limit=1 returns exactly one row.
* ``limit`` upper-bound — limit=10000 (> 5000 max) → 422.
* ``limit`` lower-bound — limit=0 → 422.
* ``since`` negative → 422.
* ``entity`` filter — restricts rows to a single entity name.
* NDJSON content-type + each line is a valid JSON object with expected keys.
* RLS / tenant scoping — rows belonging to another tenant are NOT
  returned by the default test bearer (which uses DEFAULT_TENANT).
"""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.change_log import ChangeLog

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000ffffffff")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
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


async def _append_change(
    *,
    entity: str = "contact",
    op: str = "create",
    actor: str = "pytest",
    payload: dict | None = None,
    version: int = 1,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
    entity_id: uuid.UUID | None = None,
) -> int:
    """Insert a ChangeLog row directly, return its id."""
    async with AsyncSessionLocal() as session:
        row = ChangeLog(
            tenant_id=tenant_id,
            entity=entity,
            entity_id=entity_id or uuid.uuid4(),
            op=op,
            actor=actor,
            payload=payload or {"note": "pytest"},
            version=version,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _purge_changes() -> None:
    """Clear all change_log rows for both default + other tenant so the
    NDJSON feed counts are deterministic."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ChangeLog))
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_changes() -> None:
    await _purge_changes()
    yield
    await _purge_changes()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_requires_bearer(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.get("/api/v1/changes")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Happy path — empty + populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_feed_returns_empty_body(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/changes")
    assert resp.status_code == 200, resp.text
    assert resp.text == ""
    assert resp.headers.get("x-row-count") == "0"
    # X-Cursor-Next echoes the input ``since`` (default 0).
    assert resp.headers.get("x-cursor-next") == "0"
    assert resp.headers.get("content-type", "").startswith("application/x-ndjson")


@pytest.mark.asyncio
async def test_populated_feed_returns_ndjson(api_client: AsyncClient) -> None:
    id_a = await _append_change(entity="contact", op="create")
    id_b = await _append_change(entity="contact", op="update")
    resp = await api_client.get("/api/v1/changes")
    assert resp.status_code == 200, resp.text
    lines = [line for line in resp.text.split("\n") if line]
    assert len(lines) == 2
    assert resp.headers["x-row-count"] == "2"
    assert resp.headers["x-cursor-next"] == str(id_b)
    # Each line must be a JSON object with the canonical keys.
    payload = json.loads(lines[0])
    assert payload["id"] == id_a
    assert payload["entity"] == "contact"
    assert payload["op"] == "create"
    assert "entity_id" in payload
    assert "at" in payload


# ---------------------------------------------------------------------------
# `since` cursor semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_advances_past_cursor(api_client: AsyncClient) -> None:
    id_a = await _append_change(entity="contact")
    id_b = await _append_change(entity="contact")
    id_c = await _append_change(entity="contact")
    resp = await api_client.get(f"/api/v1/changes?since={id_a}")
    rows = [json.loads(l) for l in resp.text.split("\n") if l]
    assert {r["id"] for r in rows} == {id_b, id_c}
    assert resp.headers["x-cursor-next"] == str(id_c)


@pytest.mark.asyncio
async def test_since_negative_422(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/changes?since=-1")
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# `limit` clamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_one_returns_one_row(api_client: AsyncClient) -> None:
    for _ in range(3):
        await _append_change(entity="contact")
    resp = await api_client.get("/api/v1/changes?limit=1")
    rows = [json.loads(l) for l in resp.text.split("\n") if l]
    assert len(rows) == 1
    assert resp.headers["x-row-count"] == "1"


@pytest.mark.asyncio
async def test_limit_zero_422(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/changes?limit=0")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_limit_over_max_422(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/changes?limit=10000")
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# `entity` filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_filter_narrows_rows(api_client: AsyncClient) -> None:
    await _append_change(entity="contact", op="create")
    await _append_change(entity="invoice", op="create")
    await _append_change(entity="invoice", op="post")
    resp = await api_client.get("/api/v1/changes?entity=invoice")
    rows = [json.loads(l) for l in resp.text.split("\n") if l]
    assert len(rows) == 2
    for r in rows:
        assert r["entity"] == "invoice"


# ---------------------------------------------------------------------------
# Tenant isolation (defence-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tenant_rows_invisible(api_client: AsyncClient) -> None:
    """A row written under a non-default tenant id MUST NOT surface via
    the default test bearer (which authenticates as the default tenant)."""
    # Insert a foreign tenant via raw SQL (the tenants table has only
    # the default row in fresh test DBs).
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (:tid, 'pytest-other', 'pytest-other') "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(tid=_OTHER_TENANT_ID)
        )
        await session.commit()
    # Append under a foreign tenant — service filters by request tenant.
    await _append_change(tenant_id=_OTHER_TENANT_ID, entity="contact")
    await _append_change(tenant_id=_DEFAULT_TENANT_ID, entity="contact")
    resp = await api_client.get("/api/v1/changes")
    rows = [json.loads(l) for l in resp.text.split("\n") if l]
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
