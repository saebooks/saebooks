"""Contract tests for the public contact form endpoint.

Covers:
1. Valid POST inserts row + returns 200 + uuid.
2. Honeypot non-empty: returns 200 but no row inserted.
3. Invalid email: 422.
4. Message too short: 422.
5. Rate limit: 6th request from same IP within 1h returns 429.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def _reset_rate_limits() -> None:
    """Wipe rate_limit_counters so each test starts clean."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM rate_limit_counters"))
        await session.commit()


async def _purge_contact_messages() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM contact_messages"))
        await session.commit()


_VALID_PAYLOAD: dict[str, str] = {
    "name": "Test User",
    "email": "test@example.com",
    "topic": "general",
    "message": "This is a test message with enough content.",
}


# ---------------------------------------------------------------------------
# 1. Valid submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_submission_inserts_row(client: AsyncClient) -> None:
    await _reset_rate_limits()
    await _purge_contact_messages()

    resp = await client.post("/api/v1/contact/submit", json=_VALID_PAYLOAD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    msg_id = body["id"]
    # Validate it looks like a UUID
    uuid.UUID(msg_id)

    # Confirm row exists in DB
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id, name, email, topic FROM contact_messages WHERE id = :id"),
            {"id": msg_id},
        )
        row = result.first()
    assert row is not None
    assert row.name == "Test User"
    assert row.email == "test@example.com"
    assert row.topic == "general"


# ---------------------------------------------------------------------------
# 2. Honeypot non-empty — returns 200 but no row inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_honeypot_returns_200_but_no_row(client: AsyncClient) -> None:
    await _reset_rate_limits()
    await _purge_contact_messages()

    payload = {**_VALID_PAYLOAD, "website": "http://spam.example.com"}
    resp = await client.post("/api/v1/contact/submit", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True

    # No row should have been inserted
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM contact_messages"))
        count = result.scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# 3. Invalid email → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_email_returns_422(client: AsyncClient) -> None:
    await _reset_rate_limits()
    payload = {**_VALID_PAYLOAD, "email": "not-an-email"}
    resp = await client.post("/api/v1/contact/submit", json=payload)
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 4. Message too short → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_too_short_returns_422(client: AsyncClient) -> None:
    await _reset_rate_limits()
    payload = {**_VALID_PAYLOAD, "message": "short"}
    resp = await client.post("/api/v1/contact/submit", json=payload)
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 5. Rate limit: 6th request from same IP → 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_sixth_request_returns_429(client: AsyncClient) -> None:
    await _reset_rate_limits()

    statuses = []
    for i in range(6):
        payload = {
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "topic": "general",
            "message": "Test message for rate limit check — long enough.",
        }
        resp = await client.post("/api/v1/contact/submit", json=payload)
        statuses.append(resp.status_code)

    # First 5 allowed, 6th blocked
    assert statuses[0] == 200, f"First request failed: {statuses}"
    assert statuses[4] == 200, f"Fifth request failed: {statuses}"
    assert statuses[5] == 429, f"Expected 429 on 6th request, got {statuses}"
