"""Contract tests for /api/v1/healthz and /api/v1/version.

Both endpoints are intentionally unauthenticated — they are the only
non-bearer-gated routes in the v1 surface.  These tests lock that in:

* No ``Authorization`` header → 200 (not 401).
* Response shape is stable (keys, types).
* ``edition`` reflects ``saebooks.config.settings.edition``.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.config import settings
from saebooks.main import app


@pytest.fixture
async def unauth_client() -> AsyncClient:
    """Client with no Authorization header."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_healthz_no_auth_required(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["edition"] == settings.edition


async def test_healthz_ignores_bogus_bearer(unauth_client: AsyncClient) -> None:
    """A bad bearer must NOT cause 401 — the endpoint is open."""
    r = await unauth_client.get(
        "/api/v1/healthz", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert r.status_code == 200


async def test_version_no_auth_required(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["api"] == "v1"
    assert body["edition"] == settings.edition
    assert isinstance(body["version"], str)
    # Version must be non-empty and look like a dotted version string.
    assert body["version"]
    assert "." in body["version"]


async def test_version_response_shape(unauth_client: AsyncClient) -> None:
    """Lock in the exact key set so downstream clients can rely on it."""
    r = await unauth_client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"edition", "version", "api"}


async def test_healthz_response_shape(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "edition"}


async def test_healthz_does_not_appear_in_schema(unauth_client: AsyncClient) -> None:
    """/api/v1/healthz is excluded from the OpenAPI schema (noise)."""
    r = await unauth_client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    assert "/api/v1/healthz" not in paths
    # /api/v1/version SHOULD appear — it's a client-facing contract.
    assert "/api/v1/version" in paths
