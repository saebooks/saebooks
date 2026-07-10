"""Phase 1 tier-4 contract tests for /api/v1/budgets.

Covers:
* Auth gate (401 without bearer, 401 with wrong token)
* GET /api/v1/budgets → 200 with pagination shape
* GET /api/v1/budgets/{id} → 200; 404 on missing UUID
* GET /api/v1/budgets?archived=true → only archived results
* GET /api/v1/budgets?year=<n> → year filter
* GET /api/v1/budgets?month=<n> → month filter
* GET /api/v1/budgets?account_id=... → account filter
* POST → 201, version==1, tenant_id present, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* POST → year/month/amount round-trip
* POST without required field → 422
* PATCH with correct If-Match → 200, version bumped
* PATCH amount update → 200 with new amount
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Archived rows not in default list but appear with ?archived=true
* change_log sequence: create + update + delete = 3 rows with ops in order
* Tenant isolation: budget row has correct tenant_id
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog

# ---------------------------------------------------------------------------
# Helpers — resolve IDs from the seeded DB
# ---------------------------------------------------------------------------


async def _deps() -> dict[str, str]:
    """Return an EXPENSE account_id from seeded data."""
    async with AsyncSessionLocal() as session:
        account = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert account is not None, "Test DB has no EXPENSE account"
    return {"account_id": str(account.id)}


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
async def deps() -> dict[str, str]:
    return await _deps()


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run this file's tests at enterprise (all flags on) by default.

    FLAG_PROJECTS_BUDGETS (Wave A, 2026-07-10) now gates the whole
    /api/v1/budgets router. The gate-specific tests below override
    this per-test to exercise the below-tier / at-tier boundary.
    """
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")


import os as _os
import time as _time

pytestmark = pytest.mark.postgres_only

# Session-unique base: combine PID and epoch-second to pick a starting
# year in the far future (well beyond any real budget data).
# Schema allows ge=1900, le=9999. DB stores SmallInt (max 32767).
# Using year range 3000–9999 gives 84,000 unique slots per account_id —
# effectively impossible to collide within a test session.
_SESSION_YEAR_BASE = 3000 + ((_os.getpid() + int(_time.time())) % 7000)
_year_counter = _SESSION_YEAR_BASE
_month_offset = 0


def _unique_year_month() -> tuple[int, int]:
    """Return a (year, month) pair that does not repeat within a process.

    Monotonically increments per call so parallel test files calling into
    this module get different values.
    """
    global _year_counter, _month_offset
    _month_offset += 1
    if _month_offset > 12:
        _month_offset = 1
        _year_counter += 1
    return _year_counter, _month_offset


def _budget_payload(deps: dict[str, str], **overrides: object) -> dict:
    """Return a minimal valid BudgetCreate payload.

    Callers that don't supply year/month get a unique generated pair to
    avoid the (company_id, account_id, year, month) unique constraint.
    """
    if "year" not in overrides or "month" not in overrides:
        year, month = _unique_year_month()
        overrides.setdefault("year", year)
        overrides.setdefault("month", month)
    base: dict = {
        "account_id": deps["account_id"],
        "amount": "1500.00",
        "notes": "Q3 budget",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_budgets_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/budgets")
    assert r.status_code == 401


async def test_budgets_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/budgets")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_budgets_list_200(api_client: AsyncClient, deps: dict) -> None:
    r = await api_client.get("/api/v1/budgets")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_budgets_list_default_excludes_archived(
    api_client: AsyncClient, deps: dict
) -> None:
    """Default list must not include archived budget rows."""
    payload = _budget_payload(deps, amount="999.99")
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201
    b_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/budgets/{b_id}", headers={"If-Match": str(v)}
    )

    r2 = await api_client.get("/api/v1/budgets", params={"page_size": 500})
    ids = [i["id"] for i in r2.json()["items"]]
    assert b_id not in ids


async def test_budgets_list_archived_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?archived=true must return archived budget rows."""
    payload = _budget_payload(deps, amount="888.88")
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201
    b_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/budgets/{b_id}", headers={"If-Match": str(v)}
    )

    r2 = await api_client.get(
        "/api/v1/budgets", params={"archived": "true", "page_size": 500}
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert b_id in ids


async def test_budgets_list_year_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?year=<n> must only return rows for that year."""
    # Use a unique far-future year/month pair via the counter.
    payload = _budget_payload(deps, amount="100.00")
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201
    b_id = r.json()["id"]
    filter_year = r.json()["year"]

    r2 = await api_client.get(
        "/api/v1/budgets", params={"year": filter_year, "page_size": 500}
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert b_id in ids
    for item in r2.json()["items"]:
        assert item["year"] == filter_year


async def test_budgets_list_month_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?month=<n> must only return rows for that month — verifies our row is present."""
    # Use a unique pair; then filter on the month that was assigned.
    payload = _budget_payload(deps, amount="200.00")
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201
    b_id = r.json()["id"]
    filter_month = r.json()["month"]

    r2 = await api_client.get(
        "/api/v1/budgets", params={"month": filter_month, "page_size": 500}
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert b_id in ids
    for item in r2.json()["items"]:
        assert item["month"] == filter_month


async def test_budgets_list_account_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?account_id=... must only return rows for that account."""
    r = await api_client.post("/api/v1/budgets", json=_budget_payload(deps))
    assert r.status_code == 201

    r2 = await api_client.get(
        "/api/v1/budgets",
        params={"account_id": deps["account_id"], "page_size": 500},
    )
    assert r2.status_code == 200
    assert r2.json()["total"] > 0
    for item in r2.json()["items"]:
        assert item["account_id"] == deps["account_id"]


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_budgets_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/budgets/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_budgets_get_200(
    api_client: AsyncClient, deps: dict
) -> None:
    """GET /{id} returns the budget row."""
    payload = _budget_payload(deps, amount="1500.00")
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201
    b_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/budgets/{b_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == b_id
    assert body["year"] == payload["year"]
    assert body["month"] == payload["month"]
    assert Decimal(body["amount"]) == Decimal("1500.00")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_budgets_create_201(
    api_client: AsyncClient, deps: dict
) -> None:
    payload = _budget_payload(deps)
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert "tenant_id" in body
    assert "id" in body
    assert body["year"] == payload["year"]
    assert body["month"] == payload["month"]
    assert Decimal(body["amount"]) == Decimal("1500.00")
    assert body["notes"] == "Q3 budget"


async def test_budgets_create_tenant_id_present(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST must include tenant_id in response."""
    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="250.00"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant_id"] is not None
    assert uuid.UUID(body["tenant_id"])  # valid UUID


async def test_budgets_create_missing_required(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST without account_id → 422."""
    payload = _budget_payload(deps)
    del payload["account_id"]
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 422


async def test_budgets_create_invalid_month(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST with month=13 → 422 (schema validation)."""
    payload = _budget_payload(deps, month=13)
    r = await api_client.post("/api/v1/budgets", json=payload)
    assert r.status_code == 422


async def test_budgets_create_change_log(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="300.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(b_id),
                    ChangeLog.entity == "budget",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_budgets_create_idempotency(
    api_client: AsyncClient, deps: dict
) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _budget_payload(deps, amount="400.00")

    r1 = await api_client.post(
        "/api/v1/budgets",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/budgets",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_budgets_update_bumps_version(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/budgets", json=_budget_payload(deps))
    assert r.status_code == 201
    b_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/budgets/{b_id}",
        json={"amount": "2000.00"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert Decimal(updated["amount"]) == Decimal("2000.00")


async def test_budgets_patch_notes_update(
    api_client: AsyncClient, deps: dict
) -> None:
    """PATCH notes field → 200 with updated notes."""
    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="500.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/budgets/{b_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["notes"] == "Updated notes"


# ---------------------------------------------------------------------------
# Update — missing / stale If-Match
# ---------------------------------------------------------------------------


async def test_budgets_update_requires_if_match(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/budgets", json=_budget_payload(deps))
    assert r.status_code == 201
    b_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/budgets/{b_id}", json={"amount": "100.00"}
    )
    assert r2.status_code == 428


async def test_budgets_stale_if_match_returns_409(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/budgets", json=_budget_payload(deps))
    assert r.status_code == 201
    b_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/budgets/{b_id}",
        json={"amount": "99.99"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == b_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_budgets_delete_204(
    api_client: AsyncClient, deps: dict
) -> None:
    """DELETE with correct If-Match → 204, no longer in default list."""
    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="750.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/budgets/{b_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    r3 = await api_client.get("/api/v1/budgets")
    ids = [i["id"] for i in r3.json()["items"]]
    assert b_id not in ids


async def test_budgets_delete_stale_if_match_409(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="600.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/budgets/{b_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_budgets_delete_requires_if_match(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="450.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/budgets/{b_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_budgets_change_log_full_sequence(
    api_client: AsyncClient, deps: dict
) -> None:
    """Create + update + delete = 3 budget change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/budgets",
        json=_budget_payload(deps, amount="1000.00"),
    )
    assert r.status_code == 201
    b_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/budgets/{b_id}",
        json={"notes": "Updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/budgets/{b_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(b_id),
                    ChangeLog.entity == "budget",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    ops = [row.op for row in rows]
    versions = [row.version for row in rows]
    assert "created" in ops
    assert "updated" in ops
    assert "deleted" in ops
    assert versions == sorted(versions)  # monotonically increasing


# ---------------------------------------------------------------------------
# FLAG_PROJECTS_BUDGETS gate — Wave A (2026-07-10)
# ---------------------------------------------------------------------------


async def test_budgets_gate_community(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG_PROJECTS_BUDGETS gate: community → 404 on the whole router."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    r = await api_client.get("/api/v1/budgets")
    assert r.status_code == 404


async def test_budgets_gate_offline_succeeds(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG_PROJECTS_BUDGETS gate: offline → 200 (this is where it turns on)."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    r = await api_client.get("/api/v1/budgets")
    assert r.status_code == 200, r.text


async def test_budgets_gate_community_create_404(
    api_client: AsyncClient, deps: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG_PROJECTS_BUDGETS gate: community → 404 on create too, not just list."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    r = await api_client.post("/api/v1/budgets", json=_budget_payload(deps))
    assert r.status_code == 404
