"""Round-2 audit fix — X-Company-Id is honoured across all v1 routers.

Each previously-private ``_first_company_id`` helper has been
replaced with the shared ``get_active_company_id`` dependency from
``saebooks.api.v1.deps``. This file verifies — one positive test per
router — that:

* When ``X-Company-Id`` points at a tenant-owned company, the request
  is scoped to that company (e.g. list endpoints return data for the
  pinned company, not the seed company; create endpoints persist into
  the pinned company).
* When ``X-Company-Id`` is malformed → 400.
* When ``X-Company-Id`` does not belong to the tenant → 404.

A single second company is provisioned per test in the same tenant so
the back-compat fallback (first by created_at) still picks the seed
company when the header is absent. The seed company's ``created_at``
is pinned to epoch by the session-wide ``seed_coa`` fixture in
``tests/conftest.py``.

These are the smallest possible smoke tests — they prove the header
plumbing reaches the router, not the breadth of router behaviour.
Each router already has a comprehensive contract suite elsewhere.

Lane 1 P0-1, Lane 2 P0-2, Lane 4 P1 from the 2026-05-23 critic run.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.bank_rule import BankRule
from saebooks.models.company import Company

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Shared fixtures
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


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run this file's tests at enterprise (all flags on) by default.

    ``_LIST_ROUTES`` below includes ``/api/v1/budgets`` and
    ``/api/v1/projects``, both gated by FLAG_PROJECTS_BUDGETS
    (Wave A, 2026-07-10) — this sweep asserts 400/404/200 status
    codes coming from ``get_active_company_id``, not from the feature
    gate, so every route here needs a tier that has the flag. Harmless
    no-op for the other (ungated) routes in the list.
    """
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")


@pytest.fixture
async def seed_company_id() -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        c = result.scalars().first()
        assert c is not None, "test DB must have a seed company"
        return str(c.id)


@pytest.fixture
async def other_company_id() -> str:
    """Provision a second active company in the same tenant.

    Sorted later than the seed company by ``created_at`` so the
    fallback path (first by created_at) still picks the seed company
    when no ``X-Company-Id`` is sent.
    """
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                tenant_id=_DEFAULT_TENANT_ID,
                name=f"X-Company-Id Test Co {cid.hex[:8]}",
                base_currency="AUD",
                fin_year_start_month=7,
                audit_mode="immutable",
            )
        )
        await session.commit()
    yield str(cid)
    # Cleanup — best-effort
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, cid)
        if co is not None:
            await session.delete(co)
            await session.commit()


# ---------------------------------------------------------------------------
# Generic 400 / 404 paths
# ---------------------------------------------------------------------------


# Every router goes through the same get_active_company_id dep. Listing the
# routes here is enough to prove the dep is wired into the router (vs. only
# applied to some handlers). Each row is (route, expected_status_no_header).
# ``expected_status`` is the status for a plain GET with no X-Company-Id, used
# to make sure we didn't break the back-compat fallback.
_LIST_ROUTES = [
    "/api/v1/account_ranges",
    "/api/v1/accounts",
    "/api/v1/bank_rules",
    "/api/v1/bills",
    "/api/v1/budgets",
    "/api/v1/contacts",
    "/api/v1/credit_notes",
    "/api/v1/invoices",
    "/api/v1/items",
    "/api/v1/journal_templates",
    "/api/v1/payments",
    "/api/v1/projects",
    "/api/v1/recurring_invoices",
    "/api/v1/search",
    "/api/v1/tax_codes",
]


@pytest.mark.parametrize("route", _LIST_ROUTES)
async def test_x_company_id_malformed_uuid_returns_400(
    api_client: AsyncClient, route: str
) -> None:
    """X-Company-Id with a non-UUID string returns 400 from get_active_company_id."""
    resp = await api_client.get(route, headers={"X-Company-Id": "not-a-uuid"})
    assert resp.status_code == 400, (
        f"{route}: expected 400 for malformed X-Company-Id, got "
        f"{resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.parametrize("route", _LIST_ROUTES)
async def test_x_company_id_unknown_uuid_returns_404(
    api_client: AsyncClient, route: str
) -> None:
    """X-Company-Id pointing at a non-existent UUID returns 404."""
    bogus = str(uuid.uuid4())
    resp = await api_client.get(route, headers={"X-Company-Id": bogus})
    assert resp.status_code == 404, (
        f"{route}: expected 404 for unknown X-Company-Id, got "
        f"{resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.parametrize("route", _LIST_ROUTES)
async def test_x_company_id_valid_returns_200(
    api_client: AsyncClient, route: str, other_company_id: str
) -> None:
    """X-Company-Id pointing at a tenant-owned company returns 200."""
    resp = await api_client.get(
        route, headers={"X-Company-Id": other_company_id}
    )
    assert resp.status_code == 200, (
        f"{route}: expected 200 when pinning a real second company, got "
        f"{resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# Reconciliation router — list endpoint lives under /accounts so it isn't
# in _LIST_ROUTES (which assumes root-list shape).
# ---------------------------------------------------------------------------


async def test_reconciliation_accounts_honours_x_company_id(
    api_client: AsyncClient, other_company_id: str
) -> None:
    """``/api/v1/reconciliation/accounts`` honours X-Company-Id."""
    bogus = str(uuid.uuid4())
    r_bad = await api_client.get(
        "/api/v1/reconciliation/accounts",
        headers={"X-Company-Id": bogus},
    )
    assert r_bad.status_code == 404

    r_ok = await api_client.get(
        "/api/v1/reconciliation/accounts",
        headers={"X-Company-Id": other_company_id},
    )
    assert r_ok.status_code == 200
    # Newly-created company has no accounts at all — empty list.
    assert r_ok.json() == []


# ---------------------------------------------------------------------------
# Companies router — list ignores X-Company-Id (lists *all* tenant companies),
# so the equivalent positive test is the existing trio in test_companies.py:
#   test_x_company_id_header_invalid_uuid_returns_400
#   test_x_company_id_header_unknown_uuid_returns_404
#   test_x_company_id_header_valid_uuid_returns_200
# (those use /api/v1/contacts as the probe). We do NOT duplicate them here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Positive write test — create a bank rule pinned to the second company.
# Proves X-Company-Id reaches a POST handler, not only GET handlers.
# ---------------------------------------------------------------------------


async def test_bank_rule_create_lands_in_pinned_company(
    api_client: AsyncClient, other_company_id: str, seed_company_id: str
) -> None:
    """POST /bank_rules with X-Company-Id persists into the pinned company."""
    # Grab any EXPENSE account from the seed company — bank_rules.account_id
    # is FK'd to accounts. Cross-company-FK is allowed at the DB level
    # (no FK constraint on company match), and bank_rules service does
    # NOT enforce same-company on account_id. So we can reference a seed
    # account from the other company's rule; the test only proves the
    # rule lands with company_id == other_company_id.
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(Account.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        assert acct is not None
        acct_id = str(acct.id)

    body = {
        "name": f"x-cid-sweep-{uuid.uuid4().hex[:8]}",
        "match_pattern": "X_COMPANY_ID_SWEEP_TEST",
        "match_type": "CONTAINS",
        "account_id": acct_id,
        "auto_create": False,
        "priority": 0,
        "is_active": True,
    }
    resp = await api_client.post(
        "/api/v1/bank_rules",
        json=body,
        headers={"X-Company-Id": other_company_id},
    )
    assert resp.status_code == 201, f"create failed: {resp.text[:300]}"
    rule_id = uuid.UUID(resp.json()["id"])

    # Verify in the DB the rule landed in the pinned company.
    async with AsyncSessionLocal() as session:
        rule = await session.get(BankRule, rule_id)
        assert rule is not None
        assert str(rule.company_id) == other_company_id, (
            f"Rule landed in {rule.company_id}, expected {other_company_id} — "
            f"X-Company-Id was ignored on POST."
        )
        # The seed company's bank_rules list must NOT contain this rule
        # (proves the pinning is real, not a side-effect of the seed
        # company always being picked).
        assert str(rule.company_id) != seed_company_id

        # Cleanup the rule so the cleanup-fixture's Company.delete can succeed.
        await session.delete(rule)
        await session.commit()
