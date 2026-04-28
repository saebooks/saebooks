"""Phase 0 contract tests for /api/v1/contacts.

Covers:

* Auth gate (401 without bearer)
* Round-trip CRUD
* If-Match + 409 on stale version
* X-Idempotency-Key replays cached response
* change_log row appears for each write
* /api/v1/changes cursor pagination
* /api/v1/snapshot streams NDJSON
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


def _rand_name(prefix: str = "API Test") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


async def test_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/contacts")
    assert r.status_code == 401


async def test_rejects_wrong_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer nope-this-is-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/contacts")
    assert r.status_code == 401


async def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the env var at runtime should change the expected token."""
    monkeypatch.setenv("SAEBOOKS_DEV_API_TOKEN", "runtime-token-42")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer runtime-token-42"},
    ) as ac:
        r = await ac.get("/api/v1/contacts")
    assert r.status_code == 200


async def test_full_crud_roundtrip(api_client: AsyncClient) -> None:
    # CREATE
    name = _rand_name()
    idem_create = str(uuid.uuid4())
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "CUSTOMER", "email": "a@b.c"},
        headers={"X-Idempotency-Key": idem_create},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == name
    assert created["version"] == 1
    contact_id = created["id"]

    # GET
    r = await api_client.get(f"/api/v1/contacts/{contact_id}")
    assert r.status_code == 200
    assert r.json()["id"] == contact_id

    # LIST — should include our new contact
    r = await api_client.get("/api/v1/contacts", params={"q": name})
    assert r.status_code == 200
    body = r.json()
    assert any(c["id"] == contact_id for c in body["items"])

    # UPDATE
    idem_update = str(uuid.uuid4())
    r = await api_client.patch(
        f"/api/v1/contacts/{contact_id}",
        json={"phone": "0400 123 456"},
        headers={"If-Match": "1", "X-Idempotency-Key": idem_update},
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["version"] == 2
    assert updated["phone"] == "0400 123 456"

    # DELETE (soft)
    r = await api_client.delete(
        f"/api/v1/contacts/{contact_id}",
        headers={"If-Match": "2", "X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 204


async def test_update_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("NoMatch"), "contact_type": "CUSTOMER"},
    )
    cid = r.json()["id"]
    r = await api_client.patch(
        f"/api/v1/contacts/{cid}",
        json={"phone": "x"},
    )
    assert r.status_code == 428


async def test_if_match_stale_returns_409_with_current(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("Stale"), "contact_type": "SUPPLIER"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    # Send a stale If-Match on a fresh row (version is 1, we claim 99).
    r = await api_client.patch(
        f"/api/v1/contacts/{cid}",
        json={"phone": "nope"},
        headers={"If-Match": "99"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == cid
    assert body["current"]["version"] == 1


async def test_idempotent_replay_returns_cached_body(api_client: AsyncClient) -> None:
    key = str(uuid.uuid4())
    name = _rand_name("Idempotent")
    r1 = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "CUSTOMER"},
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    first = r1.json()

    # Replay the same request — server should NOT create a new contact;
    # it should return the exact same body + status.
    r2 = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("Different"), "contact_type": "SUPPLIER"},
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json() == first

    # And there should be exactly ONE contact with that name.
    r3 = await api_client.get("/api/v1/contacts", params={"q": name})
    assert len([c for c in r3.json()["items"] if c["name"] == name]) == 1


async def test_currency_code_roundtrip(api_client: AsyncClient) -> None:
    """currency_code stored and returned on create + patch (gap ETSY-2)."""
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("JPSupplier"), "contact_type": "SUPPLIER", "country": "Japan", "currency_code": "JPY"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["currency_code"] == "JPY"
    cid = data["id"]

    # PATCH updates currency
    r = await api_client.patch(
        f"/api/v1/contacts/{cid}",
        json={"currency_code": "USD"},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["currency_code"] == "USD"

    # GET returns updated value
    r = await api_client.get(f"/api/v1/contacts/{cid}")
    assert r.status_code == 200
    assert r.json()["currency_code"] == "USD"


async def test_change_log_entries_for_writes(api_client: AsyncClient) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("LogWrite"), "contact_type": "BOTH"},
    )
    cid = r.json()["id"]
    assert r.status_code == 201

    await api_client.patch(
        f"/api/v1/contacts/{cid}",
        json={"notes": "touched"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/contacts/{cid}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(ChangeLog.id > before, ChangeLog.entity_id == uuid.UUID(cid))
                .order_by(ChangeLog.id)
            )
        ).scalars().all()
    assert [r.op for r in rows] == ["create", "update", "archive"]
    assert [r.version for r in rows] == [1, 2, 3]
    assert rows[0].payload["name"].startswith("LogWrite")


async def test_changes_pagination(api_client: AsyncClient) -> None:
    # Seed 3 writes we can page over.
    ids = []
    for _ in range(3):
        r = await api_client.post(
            "/api/v1/contacts",
            json={"name": _rand_name("Pager"), "contact_type": "CUSTOMER"},
        )
        ids.append(r.json()["id"])

    # Read cursor from change_log tail.
    async with AsyncSessionLocal() as session:
        tail = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one()
    # Query for all changes since (tail - 3) — should give >= 3 rows.
    r = await api_client.get(
        "/api/v1/changes",
        params={"since": tail - 3, "limit": 100, "entity": "contact"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert int(r.headers["X-Cursor-Next"]) >= tail
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) >= 3

    # Page size limit is honoured.
    r = await api_client.get(
        "/api/v1/changes",
        params={"since": 0, "limit": 2, "entity": "contact"},
    )
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 2


async def test_snapshot_streams_ndjson(api_client: AsyncClient) -> None:
    # Make sure there's at least one contact.
    await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("Snap"), "contact_type": "CUSTOMER"},
    )
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    import json as _json
    lines = [_json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    # last line is the cursor marker
    last = lines[-1]
    assert "_cursor" in last
    assert int(r.headers["X-Cursor-Next"]) == last["_cursor"]
    # contacts entity marker must be present somewhere in the output
    entity_markers = {ln["_entity"] for ln in lines if "_entity" in ln}
    assert "contacts" in entity_markers
