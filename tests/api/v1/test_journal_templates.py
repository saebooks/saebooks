"""Contract tests for /api/v1/journal_templates.

Covers:
* Auth gate (401 without bearer)
* List — 200 with pagination fields
* Get — 200, 404
* Create — 201, correct fields returned
* Create with empty lines — 201
* Update (PATCH) — 200, name changed
* Update non-existent — 404
* Delete (soft-archive) — 204, removed from list
* Delete non-existent — 404
* Apply — 200 with suggested_lines
* Apply archived template — 422
* Tenant isolation — template belongs to company, wrong ID 404
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
async def asset_account_id() -> str:
    """Return a real ASSET account ID from the test DB."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()
    assert row is not None, "Test DB has no ASSET account"
    return str(row.id)


def _rand_name() -> str:
    return f"Template-{uuid.uuid4().hex[:6]}"


def _template_payload(asset_account_id: str, **overrides: object) -> dict:
    base: dict = {
        "name": _rand_name(),
        "description": "Created by test",
        "lines": [
            {
                "account_id": asset_account_id,
                "description": "Test debit line",
                "debit": "100.00",
                "credit": "0.00",
                "tax_code_id": None,
            },
            {
                "account_id": asset_account_id,
                "description": "Test credit line",
                "debit": "0.00",
                "credit": "100.00",
                "tax_code_id": None,
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_journal_templates_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/journal_templates")
    assert r.status_code == 401


async def test_journal_templates_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/journal_templates")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_journal_templates_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/journal_templates")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_journal_templates_list_pagination_params(api_client: AsyncClient) -> None:
    r = await api_client.get(
        "/api/v1/journal_templates", params={"limit": 5, "offset": 0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 5
    assert body["offset"] == 0


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_journal_templates_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/journal_templates/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_journal_templates_get_200(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id, name=name),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/journal_templates/{tmpl_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == tmpl_id
    assert r2.json()["name"] == name


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_journal_templates_create_201(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id, name=name),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == name
    assert body["description"] == "Created by test"
    assert len(body["lines"]) == 2
    assert body["archived_at"] is None


async def test_journal_templates_create_empty_lines(api_client: AsyncClient) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/journal_templates",
        json={"name": name, "lines": []},
    )
    assert r.status_code == 201, r.text
    assert r.json()["lines"] == []


async def test_journal_templates_create_appears_in_list(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id, name=name),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    r2 = await api_client.get(
        "/api/v1/journal_templates", params={"limit": 1000}
    )
    assert r2.status_code == 200
    ids = [t["id"] for t in r2.json()["items"]]
    assert tmpl_id in ids


# ---------------------------------------------------------------------------
# Update (PATCH)
# ---------------------------------------------------------------------------


async def test_journal_templates_update_200(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    new_name = _rand_name()
    r2 = await api_client.patch(
        f"/api/v1/journal_templates/{tmpl_id}",
        json={"name": new_name},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == new_name


async def test_journal_templates_update_not_found(api_client: AsyncClient) -> None:
    r = await api_client.patch(
        f"/api/v1/journal_templates/{uuid.uuid4()}",
        json={"name": "ghost"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete (soft-archive)
# ---------------------------------------------------------------------------


async def test_journal_templates_delete_204(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/journal_templates/{tmpl_id}")
    assert r2.status_code == 204

    # Should no longer appear in list
    r3 = await api_client.get(
        "/api/v1/journal_templates", params={"limit": 1000}
    )
    ids = [t["id"] for t in r3.json()["items"]]
    assert tmpl_id not in ids


async def test_journal_templates_delete_not_found(api_client: AsyncClient) -> None:
    r = await api_client.delete(f"/api/v1/journal_templates/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def test_journal_templates_apply_200(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    """Apply returns suggested_lines without creating a journal entry."""
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    r2 = await api_client.post(f"/api/v1/journal_templates/{tmpl_id}/apply")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["template_id"] == tmpl_id
    assert "suggested_lines" in body
    assert len(body["suggested_lines"]) == 2
    # Verify amounts came through
    debits = [Decimal(ln["debit"]) for ln in body["suggested_lines"]]
    assert Decimal("100.00") in debits


async def test_journal_templates_apply_404(api_client: AsyncClient) -> None:
    r = await api_client.post(f"/api/v1/journal_templates/{uuid.uuid4()}/apply")
    assert r.status_code == 404


async def test_journal_templates_apply_archived_422(
    api_client: AsyncClient, asset_account_id: str
) -> None:
    """Applying an archived template returns 422."""
    r = await api_client.post(
        "/api/v1/journal_templates",
        json=_template_payload(asset_account_id),
    )
    assert r.status_code == 201
    tmpl_id = r.json()["id"]

    # Archive it
    r2 = await api_client.delete(f"/api/v1/journal_templates/{tmpl_id}")
    assert r2.status_code == 204

    # Apply should fail
    r3 = await api_client.post(f"/api/v1/journal_templates/{tmpl_id}/apply")
    assert r3.status_code == 422
