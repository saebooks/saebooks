"""SUB_CONTRACTOR contact-type tests (feat/contractor-contact-type, extends PR #31).

SUB_CONTRACTOR is the MIDDLE-TIER labour-services payee (hierarchy:
contractor -> sub-contractor -> worker): someone who provides labour under
a head-contractor. Spend is OVERHEAD EXPENSE. TPAR-reportable (the app
should default is_tpar_supplier=True for SUB_CONTRACTOR; the engine flag is
the source of truth — no type->TPAR coupling in engine code).

These tests prove the ContactType.SUB_CONTRACTOR value:

* it is creatable via the JSON API and via the service layer,
* it is filterable on the list endpoint (?type=SUB_CONTRACTOR),
* it is usable as a bill payee (NOT excluded from payable flows),
* the engine model default leaves is_tpar_supplier=False for all types
  (the "default on" for SUB_CONTRACTOR is an app-layer concern, not engine);
  a payee explicitly flagged is_tpar_supplier=True IS present in the TPAR
  report (proving inclusion is driven by the flag, not by contact_type),
* the enum value is present in the live Postgres contact_type_enum
  (i.e. migration 0163 added BOTH CONTRACTOR and SUB_CONTRACTOR).
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
from saebooks.services import tpar as tpar_svc
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


def _rand_name(prefix: str = "SubContractor") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


async def _first_expense_account() -> Account:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.is_header.is_(False),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert acct is not None
    return acct


# ---------------------------------------------------------------------------
# Enum / migration
# ---------------------------------------------------------------------------


def test_sub_contractor_in_enum() -> None:
    """The Python enum exposes SUB_CONTRACTOR immediately after CONTRACTOR."""
    assert ContactType.SUB_CONTRACTOR.value == "SUB_CONTRACTOR"
    members = list(ContactType)
    assert (
        members.index(ContactType.SUB_CONTRACTOR)
        == members.index(ContactType.CONTRACTOR) + 1
    )
    assert (
        members.index(ContactType.SUB_CONTRACTOR)
        < members.index(ContactType.BOTH)
    )


async def test_sub_contractor_value_present_in_pg_type() -> None:
    """Migration 0163 added SUB_CONTRACTOR to the Postgres enum type."""
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
    assert "SUB_CONTRACTOR" in rows, rows
    assert "CONTRACTOR" in rows, rows


# ---------------------------------------------------------------------------
# API create / filter
# ---------------------------------------------------------------------------


async def test_create_sub_contractor_via_api(api_client: AsyncClient) -> None:
    name = _rand_name()
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "SUB_CONTRACTOR"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contact_type"] == "SUB_CONTRACTOR"
    assert body["version"] == 1
    # NB: ContactOut does not expose is_tpar_supplier (it is not part of the
    # API contact schema — set/inspected via the service layer). The default
    # is asserted directly in test_create_sub_contractor_via_service below.


async def test_list_filter_sub_contractor(api_client: AsyncClient) -> None:
    name = _rand_name("FilterSub")
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": name, "contact_type": "SUB_CONTRACTOR"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    r = await api_client.get(
        "/api/v1/contacts", params={"type": "SUB_CONTRACTOR", "q": name}
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(c["id"] == cid for c in items)
    assert all(c["contact_type"] == "SUB_CONTRACTOR" for c in items)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


async def test_create_sub_contractor_via_service() -> None:
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        contact = await contacts_svc.create(
            session,
            company.id,
            name=_rand_name("SvcSub"),
            contact_type=ContactType.SUB_CONTRACTOR,
        )
    assert contact.contact_type == ContactType.SUB_CONTRACTOR
    # Engine model default is False for all types; the app-layer "default
    # on for SUB_CONTRACTOR" is not wired in the engine (flag is the authority).
    assert contact.is_tpar_supplier is False


# ---------------------------------------------------------------------------
# Payable: a SUB_CONTRACTOR is usable as a bill payee (not excluded)
# ---------------------------------------------------------------------------


async def test_sub_contractor_usable_as_bill_payee(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("PayeeSub"), "contact_type": "SUB_CONTRACTOR"},
    )
    assert r.status_code == 201
    sub_id = r.json()["id"]

    account = await _first_expense_account()
    payload = {
        "contact_id": sub_id,
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Sub-contractor labour services (expense)",
        "lines": [
            {
                "description": "Subcontracted works",
                "account_id": str(account.id),
                "quantity": "1",
                "unit_price": "800.00",
                "discount_pct": "0",
            },
        ],
    }
    r = await api_client.post("/api/v1/bills", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contact_id"] == sub_id
    assert float(body["subtotal"]) == 800.00


# ---------------------------------------------------------------------------
# TPAR: SUB_CONTRACTOR defaults out of TPAR; flag still drives inclusion
# ---------------------------------------------------------------------------


async def test_sub_contractor_tpar_driven_by_flag_not_type() -> None:
    """The engine default is is_tpar_supplier=False for ALL types (the
    "default on for SUB_CONTRACTOR" is an app-layer responsibility).
    Therefore a SUB_CONTRACTOR created without an explicit flag is ABSENT
    from the TPAR report; one explicitly flagged is_tpar_supplier=True IS
    present.  Proves TPAR inclusion is always driven by the flag, not by
    contact_type alone."""
    account = await _first_expense_account()

    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        sub = await contacts_svc.create(
            session,
            company.id,
            name=_rand_name("TparSub"),
            contact_type=ContactType.SUB_CONTRACTOR,
        )  # is_tpar_supplier defaults False
        flagged = await contacts_svc.create(
            session,
            company.id,
            name=_rand_name("TparFlagged"),
            contact_type=ContactType.SUB_CONTRACTOR,
            is_tpar_supplier=True,
        )
        await session.commit()
        sub_id = sub.id
        flagged_id = flagged.id

    # Create + POST a bill to each payee via the JSON API (the same surface
    # the other tests use) so we exercise the real engine path end-to-end.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {current_token()}"},
    ) as ac:
        for cid in (sub_id, flagged_id):
            r = await ac.post(
                "/api/v1/bills",
                json={
                    "contact_id": str(cid),
                    "issue_date": "2026-04-01",
                    "due_date": "2026-05-01",
                    "lines": [
                        {
                            "description": "Subcontracted works",
                            "account_id": str(account.id),
                            "quantity": "1",
                            "unit_price": "800.00",
                            "discount_pct": "0",
                        }
                    ],
                },
            )
            assert r.status_code == 201, r.text
            bill = r.json()
            r = await ac.post(
                f"/api/v1/bills/{bill['id']}/post",
                headers={"If-Match": str(bill["version"])},
            )
            assert r.status_code in (200, 201), r.text
            bill_company_id = uuid.UUID(bill["company_id"])

    import datetime as _dt

    async with AsyncSessionLocal() as session:
        report = await tpar_svc.tpar_report(
            session,
            bill_company_id,
            from_date=_dt.date(2025, 7, 1),
            to_date=_dt.date(2026, 6, 30),
        )
    payee_ids = {p.contact_id for p in report.payees}
    assert sub_id not in payee_ids, "default SUB_CONTRACTOR must be ABSENT from TPAR"
    assert flagged_id in payee_ids, "explicitly TPAR-flagged payee must be present"
