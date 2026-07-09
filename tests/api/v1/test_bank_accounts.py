"""Phase 1 tier-4 contract tests for /api/v1/bank_accounts.

Design (a): bank accounts are a view over the accounts table where bsb IS NOT NULL.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/bank_accounts → 200 with pagination shape
* GET /api/v1/bank_accounts/{id} → 200; 404 on missing UUID
* GET /api/v1/bank_accounts/{id} → 404 for a non-bank account
* POST /api/v1/bank_accounts → 201, version==1, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Tenant isolation (bank account not visible after archive)
* Validation error: POST without required bsb → 422
* change_log sequence: create + update + delete = 3 rows with ops created/updated/deleted
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog

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


def _ba_payload(**overrides: object) -> dict:
    """Return a minimal valid BankAccountCreate payload."""
    base: dict = {
        "code": f"1-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Operating Account",
        "bsb": "063-000",
        "bank_account_number": "12345678",
        "bank_account_title": "SAE Engineering",
        "apca_user_id": "123456",
        "bank_abbreviation": "CBA",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_bank_accounts_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/bank_accounts")
    assert r.status_code == 401


async def test_bank_accounts_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/bank_accounts")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_bank_accounts_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/bank_accounts")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_bank_accounts_list_only_bank_accounts(api_client: AsyncClient) -> None:
    """Listed items must all have a bsb value (confirming filter is applied)."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/bank_accounts")
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["bsb"] is not None


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_bank_accounts_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/bank_accounts/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_bank_accounts_get_404_for_non_bank_account(api_client: AsyncClient) -> None:
    """A plain account without bsb should return 404 from the bank_accounts endpoint."""
    async with AsyncSessionLocal() as session:
        plain = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.bsb.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()

    if plain is None:
        pytest.skip("No non-bank ASSET account available in test DB")

    r = await api_client.get(f"/api/v1/bank_accounts/{plain.id}")
    assert r.status_code == 404


async def test_bank_accounts_get_200(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/bank_accounts/{ba_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == ba_id
    assert body["bsb"] == "063-000"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_bank_accounts_create_201(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["bsb"] == "063-000"
    assert body["bank_account_number"] == "12345678"
    assert body["bank_account_title"] == "SAE Engineering"
    assert body["apca_user_id"] == "123456"
    assert body["bank_abbreviation"] == "CBA"
    assert "tenant_id" in body


async def test_bank_accounts_create_change_log(api_client: AsyncClient) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ba_id),
                    ChangeLog.entity == "bank_account",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


async def test_bank_accounts_create_validation_missing_bsb(api_client: AsyncClient) -> None:
    """POST without bsb should return 422."""
    payload = _ba_payload()
    del payload["bsb"]
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_bank_accounts_create_idempotency(api_client: AsyncClient) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _ba_payload()

    r1 = await api_client.post(
        "/api/v1/bank_accounts",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/bank_accounts",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_bank_accounts_update_bumps_version(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "Updated Account Name"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Updated Account Name"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_bank_accounts_update_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}", json={"name": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bank_accounts_stale_if_match_returns_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == ba_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_bank_accounts_delete_204(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/bank_accounts")
    ids = [i["id"] for i in r3.json()["items"]]
    assert ba_id not in ids


async def test_bank_accounts_delete_stale_if_match_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_bank_accounts_delete_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/bank_accounts/{ba_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_bank_accounts_archived_not_in_list(api_client: AsyncClient) -> None:
    """Archived bank accounts must not appear in the list endpoint."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": str(v)},
    )

    r2 = await api_client.get("/api/v1/bank_accounts")
    ids = [i["id"] for i in r2.json()["items"]]
    assert ba_id not in ids


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_bank_accounts_change_log_full_sequence(api_client: AsyncClient) -> None:
    """Create + update + delete = 3 bank_account change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "Renamed Account"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ba_id),
                    ChangeLog.entity == "bank_account",
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
# Credit limit — field round-trip, available/over_limit, soft-vs-hard
# (added 2026-05-31, migration 0141_account_credit_limit)
# ---------------------------------------------------------------------------


def _card_payload(**overrides: object) -> dict:
    """A CREDIT_CARD bank-account payload (no BSB required for cards)."""
    base: dict = {
        "code": f"2-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Credit Card",
        "account_kind": "CREDIT_CARD",
    }
    base.update(overrides)
    return base


async def test_credit_limit_create_round_trip(api_client: AsyncClient) -> None:
    """POST with credit_limit + kind persists and is returned on the out body."""
    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="5000.00", credit_limit_kind="soft"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["credit_limit"] == "5000.00"
    assert body["credit_limit_kind"] == "soft"

    # GET round-trips the persisted values.
    r2 = await api_client.get(f"/api/v1/bank_accounts/{body['id']}")
    assert r2.status_code == 200
    got = r2.json()
    assert got["credit_limit"] == "5000.00"
    assert got["credit_limit_kind"] == "soft"


async def test_credit_limit_defaults_soft(api_client: AsyncClient) -> None:
    """Omitting credit_limit_kind defaults to 'soft'."""
    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="1000.00"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["credit_limit_kind"] == "soft"


async def test_credit_limit_null_when_unset(api_client: AsyncClient) -> None:
    """No credit_limit -> available/over_limit are null on the out body."""
    r = await api_client.post("/api/v1/bank_accounts", json=_card_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["credit_limit"] is None
    assert body["available"] is None
    assert body["over_limit"] is None


async def test_credit_limit_rejects_bad_kind(api_client: AsyncClient) -> None:
    """credit_limit_kind outside {soft,hard} is a 422 (Literal-validated)."""
    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="1000.00", credit_limit_kind="blocky"),
    )
    assert r.status_code == 422, r.text


async def test_credit_limit_patch_set_and_clear(api_client: AsyncClient) -> None:
    """PATCH can set the limit, change kind to hard, then clear it (null)."""
    r = await api_client.post("/api/v1/bank_accounts", json=_card_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    # Set the limit + hard kind.
    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"credit_limit": "2500.00", "credit_limit_kind": "hard"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["credit_limit"] == "2500.00"
    assert r2.json()["credit_limit_kind"] == "hard"

    # Clear the limit by sending null.
    v2 = r2.json()["version"]
    r3 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"credit_limit": None},
        headers={"If-Match": str(v2)},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["credit_limit"] is None
    # Kind is left unchanged when omitted.
    assert r3.json()["credit_limit_kind"] == "hard"


async def test_credit_limit_patch_omitted_leaves_unchanged(api_client: AsyncClient) -> None:
    """PATCH that does not mention credit_limit must not wipe it (sentinel)."""
    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="3000.00"),
    )
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    # Update only the name; credit_limit must survive.
    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "Renamed Card"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["credit_limit"] == "3000.00"


async def test_available_and_over_limit_computed(api_client: AsyncClient) -> None:
    """available = limit - owed; over_limit reflects owed > limit.

    Seeds bank-statement lines on a CREDIT_CARD so the computed owed figure
    is non-zero, then checks the list (?include_statement_balance) and the
    detail GET both surface available/over_limit with the correct sign.
    """
    from datetime import date as _date

    from saebooks.models.bank_statement import BankStatementLine

    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="5000.00", credit_limit_kind="soft"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    ba_id = body["id"]
    company_id = uuid.UUID(body["company_id"])
    tenant_id = uuid.UUID(body["tenant_id"])

    # Seed a net -3887.53 statement balance (money spent on the card).
    async with AsyncSessionLocal() as session:
        session.add(
            BankStatementLine(
                id=uuid.uuid4(),
                company_id=company_id,
                tenant_id=tenant_id,
                account_id=uuid.UUID(ba_id),
                txn_date=_date(2026, 1, 15),
                description="Card spend",
                amount=Decimal("-3887.53"),
            )
        )
        await session.commit()

    # Detail GET computes owed for the single account.
    r_get = await api_client.get(f"/api/v1/bank_accounts/{ba_id}")
    assert r_get.status_code == 200, r_get.text
    got = r_get.json()
    # owed = 3887.53 -> available = 5000 - 3887.53 = 1112.47, not over.
    assert Decimal(got["available"]) == Decimal("1112.47")
    assert got["over_limit"] is False

    # List handler computes the same via ?include_statement_balance.
    r_list = await api_client.get(
        "/api/v1/bank_accounts",
        params={"include_statement_balance": "true", "page_size": 500},
    )
    assert r_list.status_code == 200
    item = next(i for i in r_list.json()["items"] if i["id"] == ba_id)
    assert Decimal(item["available"]) == Decimal("1112.47")
    assert item["over_limit"] is False


async def test_over_limit_true_when_owed_exceeds(api_client: AsyncClient) -> None:
    """When owed > limit, over_limit is True and available goes negative."""
    from datetime import date as _date

    from saebooks.models.bank_statement import BankStatementLine

    r = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="1000.00", credit_limit_kind="soft"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    ba_id = body["id"]

    async with AsyncSessionLocal() as session:
        session.add(
            BankStatementLine(
                id=uuid.uuid4(),
                company_id=uuid.UUID(body["company_id"]),
                tenant_id=uuid.UUID(body["tenant_id"]),
                account_id=uuid.UUID(ba_id),
                txn_date=_date(2026, 1, 20),
                description="Over-limit spend",
                amount=Decimal("-1500.00"),
            )
        )
        await session.commit()

    r_get = await api_client.get(f"/api/v1/bank_accounts/{ba_id}")
    assert r_get.status_code == 200, r_get.text
    got = r_get.json()
    assert Decimal(got["available"]) == Decimal("-500.00")
    assert got["over_limit"] is True


async def test_soft_vs_hard_persisted_distinctly(api_client: AsyncClient) -> None:
    """A hard-limit card and a soft-limit card keep their distinct kinds."""
    r_soft = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="100.00", credit_limit_kind="soft"),
    )
    r_hard = await api_client.post(
        "/api/v1/bank_accounts",
        json=_card_payload(credit_limit="100.00", credit_limit_kind="hard"),
    )
    assert r_soft.status_code == 201
    assert r_hard.status_code == 201
    assert r_soft.json()["credit_limit_kind"] == "soft"
    assert r_hard.json()["credit_limit_kind"] == "hard"


# ---------------------------------------------------------------------------
# show_on_invoice — Remit-to designation (0171)
# ---------------------------------------------------------------------------


async def test_bank_accounts_show_on_invoice_defaults_false(
    api_client: AsyncClient,
) -> None:
    """Omitting show_on_invoice on create leaves it false."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201, r.text
    assert r.json()["show_on_invoice"] is False


async def test_bank_accounts_show_on_invoice_create_true(
    api_client: AsyncClient,
) -> None:
    """show_on_invoice=true on create round-trips in the response body."""
    r = await api_client.post(
        "/api/v1/bank_accounts", json=_ba_payload(show_on_invoice=True)
    )
    assert r.status_code == 201, r.text
    assert r.json()["show_on_invoice"] is True


async def test_bank_accounts_show_on_invoice_patch_round_trip(
    api_client: AsyncClient,
) -> None:
    """PATCH toggles the flag on and off."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"show_on_invoice": True},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["show_on_invoice"] is True

    r3 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"show_on_invoice": False},
        headers={"If-Match": str(v + 1)},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["show_on_invoice"] is False


async def test_bank_accounts_show_on_invoice_is_exclusive(
    api_client: AsyncClient,
) -> None:
    """Flagging account B clears the flag on account A (single flag per company)."""
    r_a = await api_client.post(
        "/api/v1/bank_accounts", json=_ba_payload(show_on_invoice=True)
    )
    assert r_a.status_code == 201
    a_id = r_a.json()["id"]
    assert r_a.json()["show_on_invoice"] is True

    r_b = await api_client.post(
        "/api/v1/bank_accounts", json=_ba_payload(show_on_invoice=True)
    )
    assert r_b.status_code == 201
    b_id = r_b.json()["id"]
    assert r_b.json()["show_on_invoice"] is True

    r_a2 = await api_client.get(f"/api/v1/bank_accounts/{a_id}")
    assert r_a2.status_code == 200
    assert r_a2.json()["show_on_invoice"] is False, (
        "creating B with show_on_invoice=true must clear the flag on A"
    )

    # PATCH path enforces the same invariant: re-flag A → B is cleared.
    v_a = r_a2.json()["version"]
    r_a3 = await api_client.patch(
        f"/api/v1/bank_accounts/{a_id}",
        json={"show_on_invoice": True},
        headers={"If-Match": str(v_a)},
    )
    assert r_a3.status_code == 200, r_a3.text
    assert r_a3.json()["show_on_invoice"] is True

    r_b2 = await api_client.get(f"/api/v1/bank_accounts/{b_id}")
    assert r_b2.json()["show_on_invoice"] is False

    # Cleanup: leave no flagged account behind for unrelated tests.
    r_a4 = await api_client.patch(
        f"/api/v1/bank_accounts/{a_id}",
        json={"show_on_invoice": False},
        headers={"If-Match": str(r_a3.json()["version"])},
    )
    assert r_a4.status_code == 200
