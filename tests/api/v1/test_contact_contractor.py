"""CONTRACTOR contact-type tests (feat/contractor-contact-type).

Richard asked for contractors and suppliers to be two distinct contact
types. These tests prove the new ContactType.CONTRACTOR value:

* is creatable via the JSON API and via the service layer,
* is filterable on the list endpoint (?type=CONTRACTOR),
* round-trips on update,
* is usable as a bill payee (NOT excluded from payable flows),
* the enum value is present in the live Postgres contact_type_enum
  (i.e. migration 0163 applied).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import ContactType
from saebooks.services import contacts as contacts_svc
from saebooks.services.companies import ensure_seed_company

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


def _rand_name(prefix: str = "Contractor") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Enum / migration
# ---------------------------------------------------------------------------


def test_contractor_in_enum() -> None:
    """The Python enum exposes CONTRACTOR between SUPPLIER and BOTH."""
    assert ContactType.CONTRACTOR.value == "CONTRACTOR"
    members = list(ContactType)
    assert members.index(ContactType.CONTRACTOR) > members.index(ContactType.SUPPLIER)
    assert members.index(ContactType.CONTRACTOR) < members.index(ContactType.BOTH)


async def test_contractor_value_present_in_pg_type() -> None:
    """Migration 0163 added CONTRACTOR to the Postgres enum type."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'contact_type_enum'"
                )
            )
        ).scalars().all()
    assert "CONTRACTOR" in rows, rows


# ---------------------------------------------------------------------------
# API create / filter / update
# ---------------------------------------------------------------------------


async def test_create_contractor_via_api(api_client: AsyncClient) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "CONTRACTOR"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contact_type"] == "CONTRACTOR"
    assert body["version"] == 1


async def test_list_filter_contractor(api_client: AsyncClient) -> None:
    name = _rand_name("FilterCtr")
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "CONTRACTOR"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    r = await api_client.get("/api/v1/contacts", params={"type": "CONTRACTOR", "q": name})
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(c["id"] == cid for c in items)
    assert all(c["contact_type"] == "CONTRACTOR" for c in items)


async def test_update_to_contractor(api_client: AsyncClient) -> None:
    """A SUPPLIER can be reclassified to CONTRACTOR via PATCH."""
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("ReclassToCtr"), "contact_type": "SUPPLIER"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    r = await api_client.patch(
        f"/api/v1/contacts/{cid}",
        json={"contact_type": "CONTRACTOR"},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact_type"] == "CONTRACTOR"


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


async def test_create_contractor_via_service() -> None:
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        contact = await contacts_svc.create(
            session,
            company.id,
            name=_rand_name("SvcCtr"),
            contact_type=ContactType.CONTRACTOR,
            is_tpar_supplier=True,
        )
    assert contact.contact_type == ContactType.CONTRACTOR
    assert contact.is_tpar_supplier is True


# ---------------------------------------------------------------------------
# Payable: a CONTRACTOR is usable as a bill payee (not excluded)
# ---------------------------------------------------------------------------


async def test_contractor_usable_as_bill_payee(api_client: AsyncClient) -> None:
    # Create a CONTRACTOR contact via the API.
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("PayeeCtr"), "contact_type": "CONTRACTOR"},
    )
    assert r.status_code == 201
    contractor_id = r.json()["id"]

    # Find an expense account to post the bill against.
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.is_header.is_(False),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert expense is not None

    payload = {
        "contact_id": contractor_id,
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Sub-contract labour",
        "lines": [
            {
                "description": "Site labour",
                "account_id": str(expense.id),
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
            },
        ],
    }
    r = await api_client.post("/api/v1/bills", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contact_id"] == contractor_id
    assert float(body["subtotal"]) == 500.00
