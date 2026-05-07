"""Phase 1 contract tests for /api/v1/credit_notes.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/credit_notes → 200 with pagination shape
* GET /api/v1/credit_notes/{id} → 200 with lines; 404 on missing UUID
* POST /api/v1/credit_notes → 201, version==1, change_log row created
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-void)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* change_log sequence: create + update = 2 rows; full sequence = 3 rows
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact


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


@pytest.fixture
async def cn_deps() -> dict[str, str]:
    """Return IDs needed to build a credit note payload."""
    async with AsyncSessionLocal() as session:
        # Use any INCOME account for the line
        account = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()

    assert account is not None, "Test DB has no INCOME account"
    assert contact is not None, "Test DB has no contact"
    return {
        "account_id": str(account.id),
        "contact_id": str(contact.id),
    }


def _cn_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-15",
        "reason": "Goods returned",
        "notes": "Test credit note",
        "lines": [
            {
                "description": "Returned item A",
                "account_id": deps["account_id"],
                "quantity": "2",
                "unit_price": "50.00",
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_credit_notes_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/credit_notes")
    assert r.status_code == 401


async def test_credit_notes_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/credit_notes")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_credit_notes_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/credit_notes")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_credit_notes_list_filter_by_status(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/credit_notes", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_credit_notes_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/credit_notes/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_credit_notes_get_200_with_lines(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/credit_notes/{cn_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == cn_id
    assert "lines" in body
    assert isinstance(body["lines"], list)
    assert len(body["lines"]) == 1


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_credit_notes_create_201(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert "tenant_id" in body
    assert body["reason"] == "Goods returned"
    assert len(body["lines"]) == 1


async def test_credit_notes_create_change_log(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """POST should produce a change_log row with op=create, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(cn_id),
                    ChangeLog.entity == "credit_note",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_credit_notes_update_bumps_version(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/credit_notes/{cn_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["notes"] == "Updated notes"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_credit_notes_update_requires_if_match(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/credit_notes/{cn_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_credit_notes_stale_if_match_returns_409(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/credit_notes/{cn_id}",
        json={"notes": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == cn_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (void / soft-delete) → 204
# ---------------------------------------------------------------------------


async def test_credit_notes_void_204(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/credit_notes/{cn_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/credit_notes")
    ids = [i["id"] for i in r3.json()["items"]]
    assert cn_id not in ids


async def test_credit_notes_delete_stale_if_match_409(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/credit_notes/{cn_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_credit_notes_delete_requires_if_match(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/credit_notes/{cn_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_credit_notes_change_log_create_update(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Create + update produces 2 change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/credit_notes/{cn_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(cn_id),
                    ChangeLog.entity == "credit_note",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 2
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[1].op == "update"
    assert rows[1].version == 2


async def test_credit_notes_change_log_full_sequence(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Create + update + void = 3 change_log rows with versions 1, 2, 3."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201
    cn_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/credit_notes/{cn_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/credit_notes/{cn_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(cn_id),
                    ChangeLog.entity == "credit_note",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "credit_note"


# ---------------------------------------------------------------------------
# POST /{id}/post — status transitions
# ---------------------------------------------------------------------------


async def test_cn_post_transitions_to_posted(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """DRAFT → POSTED via /post: returns 200 with POSTED status and bumped version."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "POSTED"
    assert body["version"] == v + 1
    assert body["id"] == cn_id


async def test_cn_post_already_posted_422(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Posting an already-POSTED credit note must return 422."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]
    v = r.json()["version"]

    # First post — should succeed
    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    v2 = r2.json()["version"]

    # Second post — already POSTED, must 422
    r3 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(v2)},
    )
    assert r3.status_code == 422


async def test_cn_post_stale_version_409(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Posting with a stale If-Match header must return 409."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]

    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == cn_id


# ---------------------------------------------------------------------------
# POST /{id}/void — status transitions
# ---------------------------------------------------------------------------


async def test_cn_void_transitions(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """POSTED → VOIDED via /void: returns 204."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]
    v = r.json()["version"]

    # First post to POSTED
    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    v2 = r2.json()["version"]

    # Now void
    r3 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/void",
        headers={"If-Match": str(v2)},
    )
    assert r3.status_code == 204


async def test_cn_void_draft_422(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Voiding a DRAFT credit note via /void must return 422."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/void",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 422


async def test_cn_void_stale_409(
    api_client: AsyncClient, cn_deps: dict[str, str]
) -> None:
    """Voiding with a stale If-Match header must return 409."""
    r = await api_client.post("/api/v1/credit_notes", json=_cn_payload(cn_deps))
    assert r.status_code == 201, r.text
    cn_id = r.json()["id"]
    v = r.json()["version"]

    # Post the credit note first
    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text

    # Attempt void with stale version
    r3 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/void",
        headers={"If-Match": "99"},
    )
    assert r3.status_code == 409
    body = r3.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == cn_id
