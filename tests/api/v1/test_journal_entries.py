"""Phase 1 contract tests for /api/v1/journal_entries.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/journal_entries → 200 with pagination shape
* GET /api/v1/journal_entries/{id} → 200 with nested lines; 404 for missing UUID
* POST /api/v1/journal_entries → 201, version==1, change_log row created
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* change_log sequence: create + update = 2 rows; full create+update+archive = 3 rows
* POST /{id}/post: DRAFT → POSTED, already-POSTED → 422, stale If-Match → 409
* POST /{id}/reverse: POSTED → REVERSED + new reversal entry, DRAFT → 422
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog


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
async def account_ids() -> dict[str, str]:
    """Return an ASSET and an EXPENSE account ID for journal line tests."""
    async with AsyncSessionLocal() as session:
        asset = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()

    assert asset is not None, "Test DB has no ASSET account"
    assert expense is not None, "Test DB has no EXPENSE account"
    return {
        "asset_id": str(asset.id),
        "expense_id": str(expense.id),
    }


def _entry_payload(account_ids: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "entry_date": "2026-04-01",
        "narration": "Test journal entry",
        "reference": None,
        "lines": [
            {
                "account_id": account_ids["asset_id"],
                "debit": "100.00",
                "credit": "0.00",
                "description": "Debit side",
            },
            {
                "account_id": account_ids["expense_id"],
                "debit": "0.00",
                "credit": "100.00",
                "description": "Credit side",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_journal_entries_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/journal_entries")
    assert r.status_code == 401


async def test_journal_entries_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/journal_entries")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_journal_entries_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/journal_entries")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_journal_entries_list_filter_by_status(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    # Create an entry (it's DRAFT by default)
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/journal_entries", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_journal_entries_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/journal_entries/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_journal_entries_get_200_with_lines(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/journal_entries/{entry_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == entry_id
    assert "lines" in body
    assert len(body["lines"]) == 2
    assert body["lines"][0]["debit"] in ("100.00", "100")
    assert body["lines"][1]["credit"] in ("100.00", "100")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_journal_entries_create_201(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert "tenant_id" in body
    assert len(body["lines"]) == 2


async def test_journal_entries_create_change_log(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """POST should produce a change_log row with op=create, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(entry_id),
                    ChangeLog.entity == "journal_entry",
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


async def test_journal_entries_update_bumps_version(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={"narration": "Updated narration"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["description"] == "Updated narration"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_journal_entries_update_requires_if_match(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}", json={"narration": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_journal_entries_stale_if_match_returns_409(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={"narration": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == entry_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (void / soft-delete) → 204
# ---------------------------------------------------------------------------


async def test_journal_entries_void_204(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/journal_entries/{entry_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/journal_entries")
    ids = [i["id"] for i in r3.json()["items"]]
    assert entry_id not in ids


async def test_journal_entries_delete_stale_if_match_409(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/journal_entries/{entry_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_journal_entries_delete_requires_if_match(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/journal_entries/{entry_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_journal_entries_change_log_create_update(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Create + update produces 2 change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={"narration": "updated"},
        headers={"If-Match": "1"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(entry_id),
                    ChangeLog.entity == "journal_entry",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 2
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[1].op == "update"
    assert rows[1].version == 2


async def test_journal_entries_change_log_full_sequence(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Create + update + void = 3 change_log rows with versions 1, 2, 3."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={"narration": "updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/journal_entries/{entry_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(entry_id),
                    ChangeLog.entity == "journal_entry",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "journal_entry"


# ---------------------------------------------------------------------------
# POST /{id}/post — status transition tests
# ---------------------------------------------------------------------------


async def test_je_post_transitions_to_posted(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """DRAFT → POSTED via /post: returns 200 with POSTED status and bumped version."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    version = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    posted = r2.json()
    assert posted["status"] == "POSTED"
    assert posted["version"] == version + 1
    assert posted["id"] == entry_id
    assert posted["posted_at"] is not None


async def test_je_post_already_posted_422(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Posting an already-POSTED entry must return 422."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    # First post
    r1 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(r.json()["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    # Second post — should fail
    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 422


async def test_je_post_stale_version_409(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Posting with a stale If-Match must return 409 with current state."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    conflict = r2.json()
    assert conflict["detail"] == "version mismatch"
    assert conflict["current"]["id"] == entry_id


# ---------------------------------------------------------------------------
# POST /{id}/reverse — status transition tests
# ---------------------------------------------------------------------------


async def test_je_reverse_creates_reversal(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """POSTED JE → /reverse returns 201 with a new reversal entry."""
    # Create and post an entry
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r1 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(r.json()["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    # Now reverse
    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/reverse",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 201, r2.text
    reversal = r2.json()

    # The reversal is a new entry (different id)
    assert reversal["id"] != entry_id
    assert reversal["status"] == "POSTED"
    assert reversal["reversal_of_id"] == entry_id

    # Debit/credit lines must be swapped
    original_lines = sorted(r1.json()["lines"], key=lambda l: l["line_no"])
    reversal_lines = sorted(reversal["lines"], key=lambda l: l["line_no"])
    assert len(reversal_lines) == len(original_lines)
    for orig, rev in zip(original_lines, reversal_lines):
        assert orig["account_id"] == rev["account_id"]
        assert orig["debit"] == rev["credit"]
        assert orig["credit"] == rev["debit"]


async def test_je_reverse_marks_original_reversed(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """After /reverse, fetching the original entry shows REVERSED status."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r1 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(r.json()["version"])},
    )
    assert r1.status_code == 200, r1.text

    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/reverse",
        headers={"If-Match": str(r1.json()["version"])},
    )
    assert r2.status_code == 201, r2.text

    # Fetch the original
    r3 = await api_client.get(f"/api/v1/journal_entries/{entry_id}")
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "REVERSED"


async def test_je_reverse_only_works_on_posted_422(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Attempting to reverse a DRAFT entry must return 422."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    version = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/reverse",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 422
