"""Phase 1 cycle 41 contract tests for /api/v1/bank_rules.

Covers:
* Auth gate (401 without bearer, 401 with wrong token)
* GET /api/v1/bank_rules → 200 with pagination shape (empty case)
* GET /api/v1/bank_rules → list with data (rule appears after create)
* GET /api/v1/bank_rules/{id} → 200; 404 on missing UUID
* POST → 201, all fields round-trip
* POST → 422 on missing required field
* POST → 422 on invalid match_type
* PATCH → 200, field updated
* PATCH → 404 for unknown rule
* DELETE → 204, rule gone from list
* DELETE → 404 for unknown rule
* POST /apply → returns {"applied": N} (N=0 when no auto_create rules)
* POST /{id}/apply → returns {"applied": N}
* Tenant isolation: rule belonging to a different company is not visible
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_rule import BankRule

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
async def expense_account_id() -> str:
    """Return an EXPENSE account ID from the seeded test DB."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
    assert row is not None, "Test DB has no EXPENSE account"
    return str(row.id)


def _rule_payload(account_id: str, **overrides: object) -> dict:
    """Return a minimal valid BankRuleCreate payload."""
    base: dict = {
        "name": f"Test Rule {uuid.uuid4().hex[:6]}",
        "match_pattern": "OFFICE",
        "match_type": "CONTAINS",
        "account_id": account_id,
        "auto_create": False,
        "priority": 0,
        "is_active": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_bank_rules_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/bank_rules")
    assert r.status_code == 401


async def test_bank_rules_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/bank_rules")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_bank_rules_list_200(api_client: AsyncClient) -> None:
    """GET /api/v1/bank_rules returns 200 with pagination shape."""
    r = await api_client.get("/api/v1/bank_rules")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)


async def test_bank_rules_list_with_data(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """A created rule appears in the list response."""
    payload = _rule_payload(expense_account_id, name=f"ListTest {uuid.uuid4().hex[:6]}")
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r2 = await api_client.get("/api/v1/bank_rules", params={"limit": 1000})
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert rule_id in ids


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_bank_rules_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/bank_rules/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_bank_rules_get_200(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    payload = _rule_payload(expense_account_id)
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/bank_rules/{rule_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == rule_id
    assert body["name"] == payload["name"]
    assert body["match_pattern"] == "OFFICE"
    assert body["match_type"] == "CONTAINS"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_bank_rules_create_201(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    payload = _rule_payload(
        expense_account_id,
        name="Office Supplies Rule",
        match_pattern="OFFICE DEPOT",
        match_type="STARTS_WITH",
        priority=5,
        is_active=True,
    )
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Office Supplies Rule"
    assert body["match_pattern"] == "OFFICE DEPOT"
    assert body["match_type"] == "STARTS_WITH"
    assert body["priority"] == 5
    assert body["is_active"] is True
    assert body["account_id"] == expense_account_id
    assert "id" in body
    assert "company_id" in body
    assert "created_at" in body
    assert "updated_at" in body


async def test_bank_rules_create_422_missing_name(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """POST without name → 422."""
    payload = _rule_payload(expense_account_id)
    del payload["name"]
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 422


async def test_bank_rules_create_422_invalid_match_type(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """POST with invalid match_type → 422."""
    payload = _rule_payload(expense_account_id, match_type="BOGUS")
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 422


async def test_bank_rules_create_422_invalid_regex(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """POST with match_type=REGEX and invalid pattern → 422."""
    payload = _rule_payload(
        expense_account_id,
        match_type="REGEX",
        match_pattern="[invalid(regex",
    )
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Update (PATCH — no If-Match; BankRule has no version column)
# ---------------------------------------------------------------------------


async def test_bank_rules_patch_200(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """PATCH updates the specified field."""
    r = await api_client.post("/api/v1/bank_rules", json=_rule_payload(expense_account_id))
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_rules/{rule_id}",
        json={"name": "Updated Name", "priority": 10},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["name"] == "Updated Name"
    assert body["priority"] == 10


async def test_bank_rules_patch_404(api_client: AsyncClient) -> None:
    """PATCH on unknown rule → 404."""
    r = await api_client.patch(
        f"/api/v1/bank_rules/{uuid.uuid4()}",
        json={"name": "Ghost"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_bank_rules_delete_204(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """DELETE returns 204 and rule no longer appears in list."""
    r = await api_client.post("/api/v1/bank_rules", json=_rule_payload(expense_account_id))
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/bank_rules/{rule_id}")
    assert r2.status_code == 204

    r3 = await api_client.get("/api/v1/bank_rules", params={"limit": 1000})
    ids = [i["id"] for i in r3.json()["items"]]
    assert rule_id not in ids


async def test_bank_rules_delete_404(api_client: AsyncClient) -> None:
    """DELETE on unknown rule → 404."""
    r = await api_client.delete(f"/api/v1/bank_rules/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Apply all rules
# ---------------------------------------------------------------------------


async def test_bank_rules_apply_all_returns_count(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """POST /apply returns {"applied": N} — zero when no auto_create rules match."""
    # Create a non-auto_create rule — should not trigger any journal entries
    payload = _rule_payload(expense_account_id, auto_create=False)
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 201

    r2 = await api_client.post("/api/v1/bank_rules/apply")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "applied" in body
    assert isinstance(body["applied"], int)
    assert body["applied"] >= 0


# ---------------------------------------------------------------------------
# Apply single rule
# ---------------------------------------------------------------------------


async def test_bank_rules_apply_single_returns_count(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """POST /{id}/apply returns {"applied": N}."""
    payload = _rule_payload(expense_account_id, match_pattern="XYZZY_NOMATCH_9z7q")
    r = await api_client.post("/api/v1/bank_rules", json=payload)
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r2 = await api_client.post(f"/api/v1/bank_rules/{rule_id}/apply")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "applied" in body
    assert isinstance(body["applied"], int)
    # No unmatched lines have this pattern — should be 0
    assert body["applied"] == 0


async def test_bank_rules_apply_single_404_for_unknown(
    api_client: AsyncClient,
) -> None:
    """POST /{id}/apply for unknown rule → 404."""
    r = await api_client.post(f"/api/v1/bank_rules/{uuid.uuid4()}/apply")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_bank_rules_tenant_isolation(
    api_client: AsyncClient, expense_account_id: str
) -> None:
    """A rule belonging to a second company is not visible via API.

    The API uses _first_company_id (ordered by created_at) to determine the
    active company.  We insert a second company with a later created_at and
    add a rule to it — the API must not return that rule.
    """
    from saebooks.models.bank_rule import MatchType
    from saebooks.models.company import Company

    foreign_company_id = uuid.uuid4()
    foreign_rule_id: uuid.UUID | None = None

    async with AsyncSessionLocal() as session:
        # Get a real account for the FK reference
        account = (
            await session.execute(
                select(Account).where(Account.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        assert account is not None

        # Create a second company (will sort after the first by created_at)
        second_company = Company(
            id=foreign_company_id,
            name="Isolation Test Co",
            base_currency="AUD",
            fin_year_start_month=7,
            audit_mode="immutable",
        )
        session.add(second_company)
        await session.flush()

        foreign_rule = BankRule(
            company_id=foreign_company_id,
            name="Foreign Company Rule",
            match_pattern="FOREIGN_ISOLATED",
            match_type=MatchType.CONTAINS,
            account_id=account.id,
            auto_create=False,
            priority=0,
            is_active=True,
        )
        session.add(foreign_rule)
        await session.commit()
        await session.refresh(foreign_rule)
        foreign_rule_id = foreign_rule.id

    # The API should not return this rule in list (it belongs to second company)
    r = await api_client.get("/api/v1/bank_rules", params={"limit": 1000})
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["items"]]
    assert str(foreign_rule_id) not in ids

    # And should return 404 on direct get
    r2 = await api_client.get(f"/api/v1/bank_rules/{foreign_rule_id}")
    assert r2.status_code == 404

    # Cleanup — remove rule then company
    async with AsyncSessionLocal() as session:
        rule_obj = await session.get(BankRule, foreign_rule_id)
        if rule_obj is not None:
            await session.delete(rule_obj)
            await session.flush()
        co = await session.get(Company, foreign_company_id)
        if co is not None:
            await session.delete(co)
        await session.commit()
