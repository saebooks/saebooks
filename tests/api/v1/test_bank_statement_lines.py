"""Phase 1 tier-4 contract tests for /api/v1/bank_statement_lines.

Individual transaction lines imported from bank statements.  Each line
belongs to a bank account (accounts.bsb IS NOT NULL).

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/bank_statement_lines → 200 with pagination shape
* GET /api/v1/bank_statement_lines/{id} → 200; 404 on missing UUID
* GET /api/v1/bank_statement_lines/{id} → 404 for archived line
* POST /api/v1/bank_statement_lines → 201, version==1, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Tenant isolation: archived line not in list
* Validation error: POST without required fields → 422
* Filter: bank_account_id filters lines to that account
* Filter: status filter returns only matching lines
* Filter: date_from/date_to filter by txn_date
* change_log sequence: create + update + delete = 3 rows with ops created/updated/deleted
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
async def bank_account_id(api_client: AsyncClient) -> str:
    """Create a bank account and return its ID for use in line tests."""
    ba_payload = {
        "code": f"B-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Bank Account for Lines",
        "bsb": "063-001",
        "bank_account_number": "99887766",
        "bank_account_title": "SAE Lines Test",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=ba_payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _line_payload(account_id: str, **overrides: object) -> dict:
    """Return a minimal valid BankStatementLineCreate payload."""
    base: dict = {
        "account_id": account_id,
        "txn_date": "2026-04-01",
        "amount": "-150.00",
        "description": "Test payment to supplier",
        "reference": "REF-001",
        "status": "UNMATCHED",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/bank_statement_lines")
    assert r.status_code == 401


async def test_bank_statement_lines_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/bank_statement_lines")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/bank_statement_lines")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert isinstance(body["items"], list)


async def test_bank_statement_lines_list_pagination_shape(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Pagination fields (limit/offset) must be echoed in the response."""
    r = await api_client.get("/api/v1/bank_statement_lines?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 10
    assert body["offset"] == 0


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/bank_statement_lines/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_bank_statement_lines_get_200(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/bank_statement_lines/{line_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == line_id
    assert body["account_id"] == bank_account_id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_create_201(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "UNMATCHED"
    assert body["amount"] == "-150.00"
    assert body["account_id"] == bank_account_id
    assert "tenant_id" in body


async def test_bank_statement_lines_create_with_balance(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """POST can include an optional running balance."""
    payload = _line_payload(bank_account_id, balance="5000.00", amount="250.00")
    r = await api_client.post("/api/v1/bank_statement_lines", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["balance"] == "5000.00"


async def test_bank_statement_lines_create_change_log(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(line_id),
                    ChangeLog.entity == "bank_statement_line",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


async def test_bank_statement_lines_create_validation_missing_fields(
    api_client: AsyncClient,
) -> None:
    """POST without required fields should return 422."""
    r = await api_client.post("/api/v1/bank_statement_lines", json={"description": "x"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_create_idempotency(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _line_payload(bank_account_id)

    r1 = await api_client.post(
        "/api/v1/bank_statement_lines",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/bank_statement_lines",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_update_bumps_version(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/bank_statement_lines/{line_id}",
        json={"status": "IGNORED", "description": "Updated description"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["status"] == "IGNORED"
    assert updated["description"] == "Updated description"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_update_requires_if_match(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_statement_lines/{line_id}", json={"description": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_stale_if_match_returns_409(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_statement_lines/{line_id}",
        json={"description": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == line_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_delete_204(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/bank_statement_lines/{line_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/bank_statement_lines")
    ids = [i["id"] for i in r3.json()["items"]]
    assert line_id not in ids


async def test_bank_statement_lines_delete_stale_if_match_409(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/bank_statement_lines/{line_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_bank_statement_lines_delete_requires_if_match(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/bank_statement_lines/{line_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Tenant isolation / archived not in list
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_archived_not_in_list(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Archived lines must not appear in the list endpoint."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/bank_statement_lines/{line_id}",
        headers={"If-Match": str(v)},
    )

    r2 = await api_client.get("/api/v1/bank_statement_lines")
    ids = [i["id"] for i in r2.json()["items"]]
    assert line_id not in ids


async def test_bank_statement_lines_get_archived_returns_404(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """GET on an archived line should return 404."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/bank_statement_lines/{line_id}",
        headers={"If-Match": str(v)},
    )

    r2 = await api_client.get(f"/api/v1/bank_statement_lines/{line_id}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_filter_by_bank_account_id(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Filter by bank_account_id must return only lines for that account."""
    # Create a line for our account
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.get(
        f"/api/v1/bank_statement_lines?bank_account_id={bank_account_id}"
    )
    assert r2.status_code == 200
    body = r2.json()
    assert any(i["id"] == line_id for i in body["items"])
    for item in body["items"]:
        assert item["account_id"] == bank_account_id


async def test_bank_statement_lines_filter_by_status(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Filter by status must return only lines with that status."""
    # Create an UNMATCHED line
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json=_line_payload(bank_account_id, status="UNMATCHED"),
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    r2 = await api_client.get(
        f"/api/v1/bank_statement_lines?bank_account_id={bank_account_id}&status=UNMATCHED"
    )
    assert r2.status_code == 200
    body = r2.json()
    assert any(i["id"] == line_id for i in body["items"])
    for item in body["items"]:
        assert item["status"] == "UNMATCHED"


async def test_bank_statement_lines_filter_by_date_range(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """date_from/date_to should filter lines by txn_date."""
    # Line in range
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json=_line_payload(bank_account_id, txn_date="2025-06-15"),
    )
    assert r.status_code == 201
    in_range_id = r.json()["id"]

    # Line out of range
    r2 = await api_client.post(
        "/api/v1/bank_statement_lines",
        json=_line_payload(bank_account_id, txn_date="2024-01-01"),
    )
    assert r2.status_code == 201
    out_of_range_id = r2.json()["id"]

    r3 = await api_client.get(
        f"/api/v1/bank_statement_lines"
        f"?bank_account_id={bank_account_id}"
        f"&date_from=2025-01-01&date_to=2025-12-31"
    )
    assert r3.status_code == 200
    ids = [i["id"] for i in r3.json()["items"]]
    assert in_range_id in ids
    assert out_of_range_id not in ids


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_bank_statement_lines_change_log_full_sequence(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Create + update + delete = 3 bank_statement_line change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/bank_statement_lines/{line_id}",
        json={"description": "Reconciled payment"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/bank_statement_lines/{line_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(line_id),
                    ChangeLog.entity == "bank_statement_line",
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
# Reconciliation — match / unmatch
# ---------------------------------------------------------------------------


async def test_match_bsl_to_payment(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """POST /match sets status=MATCHED and records matched_to_type/id."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201, r.text
    line_id = r.json()["id"]

    payment_id = str(uuid.uuid4())
    r2 = await api_client.post(
        f"/api/v1/bank_statement_lines/{line_id}/match",
        json={"matched_to_type": "PAYMENT", "matched_to_id": payment_id},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "MATCHED"
    assert body["matched_to_type"] == "PAYMENT"
    assert body["matched_to_id"] == payment_id
    assert body["matched_at"] is not None
    assert body["version"] == 2


async def test_unmatch_bsl(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """POST /unmatch clears match fields and sets status=UNMATCHED."""
    # Create and match first
    r = await api_client.post(
        "/api/v1/bank_statement_lines", json=_line_payload(bank_account_id)
    )
    assert r.status_code == 201
    line_id = r.json()["id"]

    payment_id = str(uuid.uuid4())
    r2 = await api_client.post(
        f"/api/v1/bank_statement_lines/{line_id}/match",
        json={"matched_to_type": "PAYMENT", "matched_to_id": payment_id},
    )
    assert r2.status_code == 200

    # Now unmatch
    r3 = await api_client.post(
        f"/api/v1/bank_statement_lines/{line_id}/unmatch"
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["status"] == "UNMATCHED"
    assert body["matched_to_type"] is None
    assert body["matched_to_id"] is None
    assert body["matched_at"] is None
    assert body["version"] == 3


async def test_match_nonexistent_bsl_404(api_client: AsyncClient) -> None:
    """POST /match on an unknown UUID returns 404."""
    random_id = str(uuid.uuid4())
    r = await api_client.post(
        f"/api/v1/bank_statement_lines/{random_id}/match",
        json={"matched_to_type": "PAYMENT", "matched_to_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
