"""Tests for /api/v1/imports — import wizard REST API.

Coverage:
* wizard start → step → get → commit (bank_csv full roundtrip)
* expired wizard is rejected at step and commit
* tenant isolation — wizard from tenant A is invisible to tenant B (RLS smoke)
* qbo kind requires FLAG_QBO_IMPORT (Pro+); community edition → 404
* bank_csv works without any feature flag (community edition)
* idempotency replay on POST /wizards/{id}/commit
* start + commit a coa wizard
* missing wizard_id → 404
"""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Minimal generic bank CSV accepted by the bank importer
_GENERIC_CSV = (
    "date,amount,description\n"
    "2026-04-01,100.00,Opening balance\n"
    "2026-04-02,-45.50,Test expense\n"
)

# Minimal CoA CSV
_COA_CSV = (
    "code,name,type,parent_code,tax_code_default,reconcile\n"
    "9999,Wizard Import Account,EXPENSE,,GST,false\n"
)

# QBO customer CSV
_QBO_CONTACTS_CSV = (
    "Customer,Company,First Name,Last Name,Email,Phone,"
    "Billing Address Line 1,Billing City,Billing State,Billing Zip\n"
    "Wizard Customer,,Wizard,Customer,wiz@example.com,0400000001,"
    "1 Wizard St,Wizardton,NSW,2001\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bearer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_token()}"}


@pytest.fixture
async def api_client(bearer_headers: dict[str, str]) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


@pytest.fixture
async def pro_client(
    monkeypatch: pytest.MonkeyPatch,
    bearer_headers: dict[str, str],
) -> AsyncClient:
    """Authenticated client with Pro edition active (enables FLAG_QBO_IMPORT)."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "pro")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


@pytest.fixture
async def community_client(
    monkeypatch: pytest.MonkeyPatch,
    bearer_headers: dict[str, str],
) -> AsyncClient:
    """Authenticated client with Community edition active (no QBO)."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "community")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


@pytest.fixture
async def reconcile_account() -> Account:
    """Return (or create) a reconcilable ASSET account for bank import tests."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                text(
                    "SELECT id FROM companies WHERE archived_at IS NULL "
                    "ORDER BY created_at LIMIT 1"
                )
            )
        ).first()
        assert company is not None, "Test DB has no active company"
        company_id = company[0]

        existing = (
            await session.execute(
                text(
                    "SELECT id FROM accounts WHERE company_id = :cid "
                    "AND account_type = 'ASSET' AND reconcile = true "
                    "AND archived_at IS NULL LIMIT 1"
                ).bindparams(cid=company_id)
            )
        ).first()
        if existing:
            return existing[0]

        acct_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO accounts (id, company_id, code, name, account_type, reconcile) "
                "VALUES (:aid, :cid, :code, :name, 'ASSET', true)"
            ).bindparams(
                aid=str(acct_id),
                cid=str(company_id),
                code=f"1{uuid.uuid4().int % 9000:04d}",
                name="Wizard Bank Import Account",
            )
        )
        await session.commit()
        return acct_id


# ---------------------------------------------------------------------------
# Full roundtrip — start → step → get → commit (bank_csv)
# ---------------------------------------------------------------------------


async def test_wizard_full_roundtrip_bank_csv(
    api_client: AsyncClient,
    reconcile_account: Account,
) -> None:
    account_id = str(reconcile_account)

    # --- start ---
    r = await api_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "bank_csv", "initial": {"account_id": account_id}},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    wizard_id = data["wizard_id"]
    assert data["step"] == 0
    assert data["state"]["kind"] == "bank_csv"

    # --- step: upload the raw CSV ---
    r = await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/step",
        json={"step": 0, "patch": {"raw": _GENERIC_CSV, "_completed": True}},
    )
    assert r.status_code == 200, r.text
    step_data = r.json()
    assert step_data["step"] == 1
    assert step_data["completed"] is True

    # --- get ---
    r = await api_client.get(f"/api/v1/imports/wizards/{wizard_id}")
    assert r.status_code == 200, r.text
    get_data = r.json()
    assert get_data["wizard_id"] == wizard_id
    assert get_data["state"]["raw"] == _GENERIC_CSV

    # --- commit ---
    r = await api_client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")
    assert r.status_code == 200, r.text
    commit_data = r.json()
    assert "inserted" in commit_data
    assert "total" in commit_data
    assert commit_data["total"] == 2


# ---------------------------------------------------------------------------
# Expired wizard is rejected
# ---------------------------------------------------------------------------


async def test_expired_wizard_step_rejected(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "bank_csv", "initial": {}},
    )
    assert r.status_code == 201
    wizard_id = r.json()["wizard_id"]

    # Manually expire the row.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE wizard_state SET expires_at = now() - INTERVAL '1 second' WHERE id = CAST(:wid AS uuid)"
            ).bindparams(wid=wizard_id)
        )
        await session.commit()

    r = await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/step",
        json={"step": 0, "patch": {"raw": _GENERIC_CSV}},
    )
    assert r.status_code == 410


async def test_expired_wizard_commit_rejected(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "bank_csv", "initial": {}},
    )
    assert r.status_code == 201
    wizard_id = r.json()["wizard_id"]

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE wizard_state SET expires_at = now() - INTERVAL '1 second' WHERE id = CAST(:wid AS uuid)"
            ).bindparams(wid=wizard_id)
        )
        await session.commit()

    r = await api_client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation — RLS smoke
# Wizard created by tenant A is invisible to tenant B.
# ---------------------------------------------------------------------------


async def test_tenant_isolation_wizard_invisible_to_other_tenant(
    bearer_headers: dict[str, str],
) -> None:
    """Wizard started under tenant A must be invisible to tenant B under RLS."""
    # Seed two tenants + companies using the owner engine (bypasses RLS).
    suffix = uuid.uuid4().hex[:6]
    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        # Insert tenant A
        session.add(Tenant(id=tenant_a_id, name=f"WizIso-A-{suffix}", slug=f"wiz-iso-a-{suffix}"))
        session.add(Tenant(id=tenant_b_id, name=f"WizIso-B-{suffix}", slug=f"wiz-iso-b-{suffix}"))
        await session.flush()
        # Create wizard directly under tenant A by setting the GUC.
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_a_id}'")
        )
        await session.execute(
            text(
                "INSERT INTO wizard_state (id, tenant_id, kind, state, expires_at) "
                "VALUES (CAST(:wid AS uuid), CAST(:tid AS uuid), 'bank_csv', '{}'::jsonb, now() + INTERVAL '1 hour')"
            ).bindparams(wid=str(uuid.uuid4()), tid=str(tenant_a_id))
        )
        # Retrieve the id we just inserted.
        row = await session.execute(
            text(
                "SELECT id FROM wizard_state WHERE tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ).bindparams(tid=str(tenant_a_id))
        )
        wizard_id_a = str(row.scalar_one())
        await session.commit()

    # Now attempt to access the wizard as tenant B.
    # We override SAEBOOKS_DEV_TENANT_ID to simulate tenant B's request context.
    import os
    old_tenant = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = str(tenant_b_id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=bearer_headers,
        ) as client:
            r = await client.get(f"/api/v1/imports/wizards/{wizard_id_a}")
    finally:
        if old_tenant:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = old_tenant
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # RLS policy makes tenant B see no rows → 404 not 200.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# QBO kind requires FLAG_QBO_IMPORT
# ---------------------------------------------------------------------------


async def test_qbo_wizard_start_requires_pro_flag(
    community_client: AsyncClient,
) -> None:
    r = await community_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "qbo", "initial": {}},
    )
    # require_feature returns 404 when disabled.
    assert r.status_code == 404


async def test_qbo_wizard_start_allowed_with_pro_flag(
    pro_client: AsyncClient,
) -> None:
    r = await pro_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "qbo", "initial": {}},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# bank_csv works without any feature flag (community edition)
# ---------------------------------------------------------------------------


async def test_bank_csv_wizard_works_on_community(
    community_client: AsyncClient,
    reconcile_account: Account,
) -> None:
    account_id = str(reconcile_account)
    r = await community_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "bank_csv", "initial": {"account_id": account_id}},
    )
    assert r.status_code == 201
    wizard_id = r.json()["wizard_id"]

    # Step: upload raw CSV
    r = await community_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/step",
        json={"step": 0, "patch": {"raw": _GENERIC_CSV, "_completed": True}},
    )
    assert r.status_code == 200

    # Commit
    r = await community_client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")
    assert r.status_code == 200
    assert r.json()["total"] == 2


# ---------------------------------------------------------------------------
# Idempotency replay on commit
# ---------------------------------------------------------------------------


async def test_commit_idempotency_replay(
    api_client: AsyncClient,
    reconcile_account: Account,
) -> None:
    account_id = str(reconcile_account)

    # Start and prime wizard.
    r = await api_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "bank_csv", "initial": {"account_id": account_id}},
    )
    wizard_id = r.json()["wizard_id"]

    await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/step",
        json={"step": 0, "patch": {"raw": _GENERIC_CSV}},
    )

    idem_key = str(uuid.uuid4())
    # First commit
    r1 = await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/commit",
        headers={"X-Idempotency-Key": idem_key},
    )
    assert r1.status_code == 200
    first = r1.json()

    # Replay — second call with same key.
    r2 = await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/commit",
        headers={"X-Idempotency-Key": idem_key},
    )
    assert r2.status_code == 200
    assert r2.json() == first


# ---------------------------------------------------------------------------
# Missing wizard_id → 404
# ---------------------------------------------------------------------------


async def test_get_missing_wizard_returns_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/imports/wizards/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_step_missing_wizard_returns_404(api_client: AsyncClient) -> None:
    r = await api_client.post(
        f"/api/v1/imports/wizards/{uuid.uuid4()}/step",
        json={"step": 0, "patch": {}},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Start + commit a CoA wizard
# ---------------------------------------------------------------------------


async def test_coa_wizard_commit(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/imports/wizards",
        json={"kind": "coa", "initial": {}},
    )
    assert r.status_code == 201
    wizard_id = r.json()["wizard_id"]

    # Upload the raw CoA CSV in a step.
    r = await api_client.post(
        f"/api/v1/imports/wizards/{wizard_id}/step",
        json={"step": 0, "patch": {"raw": _COA_CSV, "_completed": True}},
    )
    assert r.status_code == 200

    # Commit — should return new/changed/removed.
    r = await api_client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")
    assert r.status_code == 200
    result = r.json()
    # "new" (or "added") count should be present.
    assert any(k in result for k in ("new", "added", "changed", "removed")), (
        f"Expected at least one of new/added/changed/removed in {result}"
    )


# ---------------------------------------------------------------------------
# Unauthenticated requests are rejected
# ---------------------------------------------------------------------------


async def test_requires_bearer() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/v1/imports/wizards",
            json={"kind": "bank_csv", "initial": {}},
        )
    assert r.status_code == 401
