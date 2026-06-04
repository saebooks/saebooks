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
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
pytestmark = pytest.mark.postgres_only


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


async def test_journal_entries_list_filter_by_ref(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    needle = f"FILTERTEST-{uuid.uuid4().hex[:6].upper()}"
    payload = _entry_payload(account_ids, reference=needle)
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 201

    # Case-insensitive substring on ref.
    r2 = await api_client.get(
        "/api/v1/journal_entries", params={"ref": needle.lower()[:8]}
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] >= 1
    # Every returned entry's ref contains the needle (case-insensitive).
    for item in body["items"]:
        assert needle.lower()[:8] in (item.get("ref") or "").lower()

    # A guaranteed-miss substring returns 0.
    miss = await api_client.get(
        "/api/v1/journal_entries", params={"ref": uuid.uuid4().hex}
    )
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


async def test_journal_entries_list_filter_by_description(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    needle = f"haystack-{uuid.uuid4().hex[:8]}"
    payload = _entry_payload(account_ids, narration=f"prefix {needle} suffix")
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 201

    r2 = await api_client.get(
        "/api/v1/journal_entries", params={"description": needle.upper()}
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert needle in (item.get("description") or "")


async def test_journal_entries_list_filter_by_account_id(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    created_id = r.json()["id"]

    r2 = await api_client.get(
        "/api/v1/journal_entries",
        params={"account_id": account_ids["asset_id"]},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] >= 1
    # Our just-created entry must be in the result set.
    assert any(item["id"] == created_id for item in body["items"])
    # Every returned entry has at least one line on the asset account.
    for item in body["items"]:
        assert any(
            line["account_id"] == account_ids["asset_id"] for line in item["lines"]
        )

    # Filtering by a UUID with no lines returns nothing.
    miss = await api_client.get(
        "/api/v1/journal_entries", params={"account_id": str(uuid.uuid4())}
    )
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


async def test_journal_entries_list_filter_by_account_code(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    # Look up the asset account's code so we can filter by it.
    from saebooks.models.account import Account as _Account

    async with AsyncSessionLocal() as session:
        asset = (
            await session.execute(
                select(_Account).where(_Account.id == uuid.UUID(account_ids["asset_id"]))
            )
        ).scalars().first()
    assert asset is not None
    code = asset.code

    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201

    r2 = await api_client.get(
        "/api/v1/journal_entries", params={"account_code": code}
    )
    assert r2.status_code == 200
    assert r2.json()["total"] >= 1

    # Unknown code returns empty (no 404 — silent zero match).
    miss = await api_client.get(
        "/api/v1/journal_entries",
        params={"account_code": f"NONEXISTENT-{uuid.uuid4().hex[:8]}"},
    )
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


async def test_journal_entries_list_sort_ref_asc(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Two entries with refs ending Z and A — asc puts A first."""
    suffix = uuid.uuid4().hex[:6].upper()
    r1 = await api_client.post(
        "/api/v1/journal_entries",
        json=_entry_payload(account_ids, reference=f"SORT-{suffix}-Z"),
    )
    assert r1.status_code == 201
    r2 = await api_client.post(
        "/api/v1/journal_entries",
        json=_entry_payload(account_ids, reference=f"SORT-{suffix}-A"),
    )
    assert r2.status_code == 201

    r = await api_client.get(
        "/api/v1/journal_entries",
        params={"ref": f"SORT-{suffix}", "sort": "ref", "dir": "asc"},
    )
    assert r.status_code == 200
    refs = [item["ref"] for item in r.json()["items"]]
    a_idx = next(i for i, x in enumerate(refs) if x.endswith("-A"))
    z_idx = next(i for i, x in enumerate(refs) if x.endswith("-Z"))
    assert a_idx < z_idx, f"asc on ref should put -A before -Z; got {refs}"

    # Flip direction.
    r = await api_client.get(
        "/api/v1/journal_entries",
        params={"ref": f"SORT-{suffix}", "sort": "ref", "dir": "desc"},
    )
    refs = [item["ref"] for item in r.json()["items"]]
    a_idx = next(i for i, x in enumerate(refs) if x.endswith("-A"))
    z_idx = next(i for i, x in enumerate(refs) if x.endswith("-Z"))
    assert z_idx < a_idx, f"desc on ref should put -Z before -A; got {refs}"


async def test_journal_entries_list_sort_invalid_400(
    api_client: AsyncClient,
) -> None:
    r = await api_client.get(
        "/api/v1/journal_entries", params={"sort": "haxxor"}
    )
    assert r.status_code == 400
    r2 = await api_client.get(
        "/api/v1/journal_entries", params={"dir": "sideways"}
    )
    assert r2.status_code == 400


async def test_journal_entries_list_filter_by_posted_by(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    # Create + post an entry so posted_by is populated.
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]
    version = r.json()["version"]

    post_resp = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(version)},
    )
    assert post_resp.status_code == 200
    posted_by = post_resp.json().get("posted_by") or ""
    assert posted_by, "posted_by must be set after /post transition"

    # First few chars of posted_by — should match case-insensitively.
    needle = posted_by[:4]
    r2 = await api_client.get(
        "/api/v1/journal_entries", params={"posted_by": needle.upper()}
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert needle.lower() in (item.get("posted_by") or "").lower()


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


# ---------------------------------------------------------------------------
# Bug 1 regression: POST with unbalanced lines must return 422
# ---------------------------------------------------------------------------


async def test_create_unbalanced_je_returns_422(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """POST /api/v1/journal_entries with debit=100, credit=99 must return 422.

    Regression test for the validator gap diagnosed in audit-trail/03.
    Before this fix, the API accepted unbalanced lines silently.
    The Pydantic model_validator on JournalEntryCreate catches this at
    the schema layer before the service is even called.
    """
    payload = {
        "entry_date": "2026-04-01",
        "narration": "Unbalanced entry — should be rejected",
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
                "credit": "99.00",  # off by $1 — deliberately unbalanced
                "description": "Credit side (wrong amount)",
            },
        ],
    }
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 422, (
        f"Expected 422 for unbalanced JE, got {r.status_code}: {r.text}"
    )
    body = r.json()
    # Per saebooks 422 handler, Pydantic errors land under body["errors"]
    # (the model_validator message) while body["detail"] holds only the
    # generic problem-summary. Search the whole body for the keywords.
    body_str = str(body).lower()
    assert "unbalanced" in body_str or "debit" in body_str or "credit" in body_str, (
        f"Expected error message to mention balance, got: {body}"
    )


async def test_update_unbalanced_lines_returns_422(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """PATCH /api/v1/journal_entries/{id} with unbalanced line replacement → 422."""
    # Create a balanced entry first
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    version = r.json()["version"]

    # Now try to replace lines with unbalanced ones
    r2 = await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={
            "lines": [
                {
                    "account_id": account_ids["asset_id"],
                    "debit": "200.00",
                    "credit": "0.00",
                },
                {
                    "account_id": account_ids["expense_id"],
                    "debit": "0.00",
                    "credit": "150.00",  # off by $50
                },
            ]
        },
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 422, (
        f"Expected 422 for unbalanced line update, got {r2.status_code}: {r2.text}"
    )


# ---------------------------------------------------------------------------
# PRTR-1 regression: cross-tenant account reference must be rejected
# ---------------------------------------------------------------------------


async def test_create_je_rejects_cross_tenant_account(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """PRTR-1: POST /api/v1/journal_entries with a line referencing an account
    from a different tenant must return 422, not 201/303.

    Carousel run: 20260427T203251Z, gap PRTR-1 (cross_tenant_write).
    """
    foreign_tid = uuid.uuid4()
    foreign_cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Tenant(
            id=foreign_tid,
            name=f"ForeignTenant-PRTR1-{foreign_tid.hex[:6]}",
            slug=f"prtr1-{foreign_tid.hex[:6]}",
        ))
        await session.flush()
        session.add(Company(
            id=foreign_cid,
            tenant_id=foreign_tid,
            name=f"Foreign Corp PRTR1 {foreign_tid.hex[:6]}",
        ))
        await session.flush()
        f_asset = Account(
            company_id=foreign_cid, tenant_id=foreign_tid,
            code=f"1-{foreign_tid.hex[:4]}",
            name="Foreign Asset PRTR1",
            account_type=AccountType.ASSET,
            is_header=False,
        )
        f_expense = Account(
            company_id=foreign_cid, tenant_id=foreign_tid,
            code=f"6-{foreign_tid.hex[:4]}",
            name="Foreign Expense PRTR1",
            account_type=AccountType.EXPENSE,
            is_header=False,
        )
        session.add_all([f_asset, f_expense])
        await session.commit()
        await session.refresh(f_asset)
        await session.refresh(f_expense)

    # Mixed-tenant lines: line 1 own tenant, line 2 foreign tenant (the attack)
    payload = {
        "entry_date": "2026-04-10",
        "narration": "Cross-tenant attack (PRTR-1)",
        "lines": [
            {
                "account_id": account_ids["asset_id"],
                "debit": "100.00",
                "credit": "0.00",
                "description": "Own-tenant debit",
            },
            {
                "account_id": str(f_expense.id),
                "debit": "0.00",
                "credit": "100.00",
                "description": "Foreign-tenant credit — must be rejected",
            },
        ],
    }
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 422, (
        f"Expected 422 for cross-tenant account, got {r.status_code}: {r.text}"
    )

    # Positive control: same-tenant accounts must still work
    r2 = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r2.status_code == 201, (
        f"Same-tenant JE should succeed, got {r2.status_code}: {r2.text}"
    )


# ---------------------------------------------------------------------------
# CAFE-1: reference field must be rejected when > 32 chars
# ---------------------------------------------------------------------------


async def test_create_je_reference_too_long_returns_422(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """CAFE-1: POST with reference > 32 chars must return 422, not 500.

    Gap CAFE-1 (validation_ux), carousel run 20260427T210813Z.
    """
    payload = _entry_payload(account_ids, reference="X" * 33)
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 422, (
        f"Expected 422 for reference > 32 chars, got {r.status_code}: {r.text}"
    )


async def test_patch_je_reference_too_long_returns_422(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """CAFE-1: PATCH with reference > 32 chars must return 422, not 500."""
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201
    entry_id = r.json()["id"]
    version = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/journal_entries/{entry_id}",
        json={"reference": "Y" * 33},
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 422, (
        f"Expected 422 for reference > 32 chars on PATCH, got {r2.status_code}: {r2.text}"
    )


async def test_create_je_reference_at_max_length_accepted(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """CAFE-1 positive control: reference exactly 32 chars must be accepted."""
    unique_ref = uuid.uuid4().hex  # exactly 32 chars, unique per run
    payload = _entry_payload(account_ids, reference=unique_ref)
    r = await api_client.post("/api/v1/journal_entries", json=payload)
    assert r.status_code == 201, (
        f"Expected 201 for reference == 32 chars, got {r.status_code}: {r.text}"
    )
    assert r.json()["ref"] == unique_ref


# ---------------------------------------------------------------------------
# PSI-5: period-lock enforcement via POST /{id}/post (gap PSI-5 carousel 20260427T220251Z)
# ---------------------------------------------------------------------------


async def test_je_post_blocked_by_period_lock(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """POST /{id}/post must return 4xx when the JE date falls inside a locked period.

    Gap PSI-5 (P1): before the fix, PostingError from _check_period_lock()
    escaped api_post() uncaught and produced a 500 instead of a 4xx, leaving
    the entry DRAFT but returning no useful error to the caller. The fix
    translates PostingError → JournalEntryError so the router returns 422.

    Setup: create a fresh company with an explicit period lock at the end of
    March 2026, then try to post a JE dated 2026-03-15 (inside the lock).
    """
    from saebooks.models.company import Company
    from saebooks.models.journal import PeriodLock
    from saebooks.services import journal as journal_svc

    # Create an isolated company under the DEFAULT tenant so the dev bearer
    # token (which resolves to the default tenant) can authenticate against it.
    # Using a separate company avoids polluting the shared default company with
    # a period lock that would break other posting tests.
    _DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    lock_cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Company(
            id=lock_cid,
            tenant_id=_DEFAULT_TENANT_ID,
            name=f"PSI5 Corp {lock_cid.hex[:6]}",
        ))
        await session.flush()
        # Seed two accounts for the isolated company
        from saebooks.models.account import Account
        iso_asset = Account(
            company_id=lock_cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"1-{lock_cid.hex[:4]}",
            name="PSI5 Asset",
            account_type=AccountType.ASSET,
            is_header=False,
        )
        iso_expense = Account(
            company_id=lock_cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"6-{lock_cid.hex[:4]}",
            name="PSI5 Expense",
            account_type=AccountType.EXPENSE,
            is_header=False,
        )
        session.add_all([iso_asset, iso_expense])
        await session.flush()
        # Lock the period through 2026-03-31 for this company only
        await journal_svc.lock_period(
            session, lock_cid, date(2026, 3, 31), locked_by="test-psi5"
        )
        await session.refresh(iso_asset)
        await session.refresh(iso_expense)
        iso_asset_id = str(iso_asset.id)
        iso_expense_id = str(iso_expense.id)

    # Use the standard API client but direct it at the isolated company via header
    from saebooks.api.v1.auth import current_token
    token = current_token()
    from httpx import ASGITransport, AsyncClient as _AC
    from saebooks.main import app as _app
    async with _AC(
        transport=ASGITransport(app=_app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(lock_cid),
        },
    ) as iso_client:
        # Create a JE dated inside the locked period
        payload = {
            "entry_date": "2026-03-15",
            "narration": "PSI-5 test — locked period",
            "lines": [
                {"account_id": iso_asset_id, "debit": "100.00", "credit": "0.00"},
                {"account_id": iso_expense_id, "debit": "0.00", "credit": "100.00"},
            ],
        }
        rc = await iso_client.post("/api/v1/journal_entries", json=payload)
        assert rc.status_code == 201, rc.text
        entry_id = rc.json()["id"]
        version = rc.json()["version"]

        # Attempt to post — must be rejected with a 4xx (period is locked)
        rp = await iso_client.post(
            f"/api/v1/journal_entries/{entry_id}/post",
            headers={"If-Match": str(version)},
        )
        assert rp.status_code in range(400, 500), (
            f"Expected 4xx for posting into locked period, got {rp.status_code}: {rp.text}"
        )
        body = rp.json()
        detail_str = str(body).lower()
        assert "lock" in detail_str or "period" in detail_str, (
            f"Expected 'lock' or 'period' in error body, got: {body}"
        )

        # Entry must still be DRAFT
        rg = await iso_client.get(f"/api/v1/journal_entries/{entry_id}")
        assert rg.status_code == 200, rg.text
        assert rg.json()["status"] == "DRAFT", (
            f"Entry should remain DRAFT after rejected post, got: {rg.json()['status']}"
        )

        # Positive control: JE after the lock boundary must post successfully
        payload_ok = {
            "entry_date": "2026-04-15",
            "narration": "PSI-5 positive control",
            "lines": [
                {"account_id": iso_asset_id, "debit": "50.00", "credit": "0.00"},
                {"account_id": iso_expense_id, "debit": "0.00", "credit": "50.00"},
            ],
        }
        rok = await iso_client.post("/api/v1/journal_entries", json=payload_ok)
        assert rok.status_code == 201, rok.text
        rok_v = rok.json()["version"]
        rpost_ok = await iso_client.post(
            f"/api/v1/journal_entries/{rok.json()['id']}/post",
            headers={"If-Match": str(rok_v)},
        )
        assert rpost_ok.status_code == 200, (
            f"Expected 200 for post after lock boundary, got {rpost_ok.status_code}: {rpost_ok.text}"
        )


# ---------------------------------------------------------------------------
# FITC-4: period-lock gate + override_reason bypass (gap FITC-4 from medium-fitness-chain)
# ---------------------------------------------------------------------------


async def test_fitc4_period_lock_gate_and_override(
    api_client: AsyncClient,
    account_ids: dict[str, str],
) -> None:
    """POST /{id}/post must reject entries dated inside a locked period (FITC-4).

    Gap FITC-4 (P1): the period_locks table was empty on the dev instance so
    every entry_date was accepted. The fix seeds Q1 2026 (locked_through
    2026-03-31) and exposes override_reason in the /post request body so a
    bookkeeper can still post with an explicit justification.

    This test:
    1. Creates an isolated company with a period lock at 2026-03-31.
    2. Verifies that posting a JE dated 2026-03-15 returns 422 with a
       meaningful error message.
    3. Verifies that re-posting with override_reason succeeds (200) and
       the reason is stored on the returned entry.
    4. Positive control: posting a JE dated 2026-04-01 (after the lock)
       succeeds without any override.
    """
    from saebooks.models.account import Account
    from saebooks.models.company import Company
    from saebooks.services import journal as journal_svc

    _DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Company(
            id=cid,
            tenant_id=_DEFAULT_TENANT_ID,
            name=f"FITC4 Corp {cid.hex[:6]}",
        ))
        await session.flush()

        asset_acct = Account(
            company_id=cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"1-{cid.hex[:4]}", name="FITC4 Asset",
            account_type=AccountType.ASSET, is_header=False,
        )
        expense_acct = Account(
            company_id=cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"6-{cid.hex[:4]}", name="FITC4 Expense",
            account_type=AccountType.EXPENSE, is_header=False,
        )
        session.add_all([asset_acct, expense_acct])
        await session.flush()

        await journal_svc.lock_period(
            session, cid, date(2026, 3, 31), locked_by="test-fitc4"
        )
        await session.refresh(asset_acct)
        await session.refresh(expense_acct)
        asset_id = str(asset_acct.id)
        expense_id = str(expense_acct.id)

    from saebooks.api.v1.auth import current_token
    from httpx import ASGITransport, AsyncClient as _AC
    from saebooks.main import app as _app

    token = current_token()
    async with _AC(
        transport=ASGITransport(app=_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "X-Company-Id": str(cid)},
    ) as iso:
        # 1. Create DRAFT JE dated inside the locked period
        r_create = await iso.post("/api/v1/journal_entries", json={
            "entry_date": "2026-03-15",
            "narration": "FITC-4 backdated entry",
            "lines": [
                {"account_id": asset_id, "debit": "200.00", "credit": "0.00"},
                {"account_id": expense_id, "debit": "0.00", "credit": "200.00"},
            ],
        })
        assert r_create.status_code == 201, r_create.text
        entry_id = r_create.json()["id"]
        version = r_create.json()["version"]

        # 2. Attempt to post without override → must be rejected (period locked)
        r_block = await iso.post(
            f"/api/v1/journal_entries/{entry_id}/post",
            headers={"If-Match": str(version)},
        )
        assert r_block.status_code in range(400, 500), (
            f"FITC-4: expected 4xx for locked-period post, got {r_block.status_code}: {r_block.text}"
        )
        detail_str = str(r_block.json()).lower()
        assert "lock" in detail_str or "period" in detail_str, (
            f"FITC-4: expected 'lock' or 'period' in error body, got: {r_block.json()}"
        )

        # Entry must still be DRAFT after rejected post
        r_check = await iso.get(f"/api/v1/journal_entries/{entry_id}")
        assert r_check.json()["status"] == "DRAFT", (
            f"FITC-4: entry must remain DRAFT after rejected post"
        )

        # 3. Post with override_reason → must succeed
        r_override = await iso.post(
            f"/api/v1/journal_entries/{entry_id}/post",
            headers={"If-Match": str(version)},
            json={"override_reason": "CFO approved late entry — corrects March payroll accrual"},
        )
        assert r_override.status_code == 200, (
            f"FITC-4: expected 200 with override_reason, got {r_override.status_code}: {r_override.text}"
        )
        posted = r_override.json()
        assert posted["status"] == "POSTED", f"FITC-4: entry must be POSTED after override"
        assert posted["override_reason"] is not None, "FITC-4: override_reason must be stored"
        assert "CFO" in posted["override_reason"], (
            f"FITC-4: override_reason not persisted correctly: {posted['override_reason']}"
        )

        # 4. Positive control: JE dated after the lock posts without override
        r_after = await iso.post("/api/v1/journal_entries", json={
            "entry_date": "2026-04-01",
            "narration": "FITC-4 positive control",
            "lines": [
                {"account_id": asset_id, "debit": "100.00", "credit": "0.00"},
                {"account_id": expense_id, "debit": "0.00", "credit": "100.00"},
            ],
        })
        assert r_after.status_code == 201, r_after.text
        r_post_after = await iso.post(
            f"/api/v1/journal_entries/{r_after.json()['id']}/post",
            headers={"If-Match": str(r_after.json()["version"])},
        )
        assert r_post_after.status_code == 200, (
            f"FITC-4: post after lock boundary must succeed, got {r_post_after.status_code}: {r_post_after.text}"
        )


# ---------------------------------------------------------------------------
# Fix #11 — /source endpoint graceful when expenses table absent
# Fix #26 — JE lines have account_code and account_name populated
# Fix #27 — JE get-by-id returns source_type/source_id for invoice-derived JE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_je_lines_have_account_code_and_name(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """#26 — GET /journal_entries/{id} must return non-null account_code and
    account_name on every line.

    Before the fix, the account relationship was not eager-loaded and
    JournalLineOut did not include those fields; both came back as null.
    """
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/journal_entries/{entry_id}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "lines" in body
    assert len(body["lines"]) > 0, "JE must have at least one line"
    for line in body["lines"]:
        assert line.get("account_code") is not None, (
            f"line {line['line_no']}: account_code is null — fix #26 regression"
        )
        assert line.get("account_name") is not None, (
            f"line {line['line_no']}: account_name is null — fix #26 regression"
        )


@pytest.mark.asyncio
async def test_je_source_endpoint_returns_200_for_je_without_source(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """#11 — GET /journal_entries/{id}/source must return 200 (not 500) even
    when the expenses table does not exist in the test DB.

    The test DB is a fresh schema; expenses is an undeployed module. Before
    the fix, get_source_doc() raised UndefinedTableError on every call.
    """
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/journal_entries/{entry_id}/source")
    assert r2.status_code == 200, (
        f"/source returned {r2.status_code} — fix #11 regression: {r2.text}"
    )
    body = r2.json()
    # A manually-created JE has no source document — nulls are correct here.
    assert "type" in body
    assert "id" in body


@pytest.mark.asyncio
async def test_je_get_by_id_has_source_fields(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """#27 — GET /journal_entries/{id} must include source_type and source_id
    fields in the response (even if both are null for a manually-created JE).

    This asserts the fields exist on the schema; a separate integration test
    (requiring invoice fixtures) would assert non-null values for an
    invoice-derived JE.
    """
    r = await api_client.post("/api/v1/journal_entries", json=_entry_payload(account_ids))
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/journal_entries/{entry_id}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    # Fields must exist in the response (null is correct for a direct JE).
    assert "source_type" in body, "#27: source_type missing from JE response"
    assert "source_id" in body, "#27: source_id missing from JE response"


async def test_je_reverse_with_explicit_reversal_date(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """POST /{id}/reverse with a reversal_date body lands the mirror entry on
    that date (e.g. a 30-Jun accrual reversed on 1-Jul), not the original's."""
    r = await api_client.post(
        "/api/v1/journal_entries", json=_entry_payload(account_ids)
    )
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    orig_date = r.json()["entry_date"]

    r1 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(r.json()["version"])},
    )
    assert r1.status_code == 200, r1.text

    r2 = await api_client.post(
        f"/api/v1/journal_entries/{entry_id}/reverse",
        headers={"If-Match": str(r1.json()["version"])},
        json={"reversal_date": "2025-07-01", "override_reason": "year-end accrual reversal (test)"},
    )
    assert r2.status_code == 201, r2.text
    reversal = r2.json()
    assert reversal["entry_date"] == "2025-07-01"
    assert reversal["entry_date"] != orig_date
    assert reversal["reversal_of_id"] == entry_id
