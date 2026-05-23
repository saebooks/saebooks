"""Contract tests for /api/v1/employees.

Gap noted in the 2026-05-23 overnight regression sweep — employees router
shipped with the payroll Phase 1 work (migration 0111) with zero contract
coverage. Sensitive TFN + bank handling is the security-critical surface
here: TFN is encrypted, masked in default DTO, plaintext only via a
dedicated reveal endpoint.

Covers:
* Auth gate (401 without bearer).
* Create — 201 with ETag; default TFN status masked.
* Create — 422 missing required (no contact_id / start_date / base_rate).
* List — pagination, total, only_active default.
* List — search filter narrows results.
* Get by id — 200 / 404 missing.
* Update PATCH — 200, version bumps.
* Update — 404 missing.
* Update — If-Match stale → 412.
* Update — If-Match malformed → 400.
* Terminate — sets end_date + reason, version bumps.
* Terminate twice → 409 (already_terminated).
* TFN reveal — 404 when employee has no TFN on file.
* TFN reveal — returns plaintext when stored (encryption round-trip).
* TFN masked in default Out shape; plaintext NEVER echoed on create.
* Archive — 204, removes from default list (only_active=True is default).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.employee import Employee


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def _seed_contact_id(name: str = "Pytest Default Contact") -> uuid.UUID:
    """Return the id of an existing contact, or create one. The
    autouse `seed_default_contact` fixture in tests/conftest.py guarantees
    at least one exists, but tests creating multiple employees in this
    file want distinct contacts so the unique constraint on (company_id,
    contact_id) is not blown."""
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    Contact.tenant_id == _DEFAULT_TENANT_ID,
                    Contact.name == name,
                    Contact.archived_at.is_(None),
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.id
        c = Contact(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            name=name,
            contact_type=ContactType.BOTH,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


async def _purge_employees() -> None:
    """Hard-delete every Employee on the seed company so each test starts
    from an empty list."""
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Employee).where(Employee.company_id == company_id)
            )
        ).scalars().all()
        for row in rows:
            await session.delete(row)
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_employees() -> None:
    await _purge_employees()
    yield
    await _purge_employees()


async def _body(
    *,
    contact_name: str = "Pytest EE Contact",
    employee_number: str | None = "E001",
    start_date: str = "2026-01-15",
    employment_basis: str = "F",
    base_rate: str = "35.00",
    tfn: str | None = None,
) -> dict:
    contact_id = await _seed_contact_id(name=contact_name)
    out: dict = {
        "contact_id": str(contact_id),
        "start_date": start_date,
        "employment_basis": employment_basis,
        "base_rate": base_rate,
    }
    if employee_number:
        out["employee_number"] = employee_number
    if tfn is not None:
        out["tfn"] = tfn
        out["tfn_status"] = "PROVIDED"
    return out


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_bearer(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.get("/api/v1/employees")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_create_requires_bearer(unauth_client: AsyncClient) -> None:
    body = await _body()
    resp = await unauth_client.post("/api/v1/employees", json=body)
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Create — happy paths + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_minimal_returns_etag(api_client: AsyncClient) -> None:
    body = await _body()
    resp = await api_client.post("/api/v1/employees", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["employment_basis"] == "F"
    # base_rate is Decimal(15, 4) on the model — serialised to 4 dp.
    from decimal import Decimal as _D
    assert _D(out["base_rate"]) == _D("35.00")
    assert out["version"] == 1
    assert out["has_bank"] is False
    assert out["archived_at"] is None
    # Default DTO must not echo plaintext TFN.
    assert "tfn" not in out
    assert resp.headers.get("etag") == '"1"'


@pytest.mark.asyncio
async def test_create_with_tfn_returns_masked(api_client: AsyncClient) -> None:
    body = await _body(contact_name="Pytest TFN", tfn="123456782")
    resp = await api_client.post("/api/v1/employees", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["tfn_status"] == "PROVIDED"
    # tfn_masked surfaces a mask; the plaintext NEVER appears.
    assert out.get("tfn_masked") is not None
    assert "123456782" not in resp.text


@pytest.mark.asyncio
async def test_create_missing_required_422(api_client: AsyncClient) -> None:
    # No contact_id, start_date, base_rate, employment_basis.
    resp = await api_client.post("/api/v1/employees", json={"employee_number": "E0"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# List + filters + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/employees")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_pagination(api_client: AsyncClient) -> None:
    for i in range(3):
        body = await _body(
            contact_name=f"Pytest EE {i}", employee_number=f"E00{i+1}"
        )
        resp = await api_client.post("/api/v1/employees", json=body)
        assert resp.status_code == 201, resp.text
    page1 = await api_client.get("/api/v1/employees?limit=2&offset=0")
    page2 = await api_client.get("/api/v1/employees?limit=2&offset=2")
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2
    assert len(page2.json()["items"]) == 1


@pytest.mark.asyncio
async def test_list_search_narrows(api_client: AsyncClient) -> None:
    a = await _body(contact_name="Pytest Alice", employee_number="ALICE")
    b = await _body(contact_name="Pytest Bob", employee_number="BOB")
    await api_client.post("/api/v1/employees", json=a)
    await api_client.post("/api/v1/employees", json=b)
    resp = await api_client.get("/api/v1/employees?search=ALICE")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["employee_number"] == "ALICE"


# ---------------------------------------------------------------------------
# Get / update / If-Match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_employee(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    get_resp = await api_client.get(f"/api/v1/employees/{eid}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == eid


@pytest.mark.asyncio
async def test_get_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.get(f"/api/v1/employees/{missing}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_bumps_version(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    e = create_resp.json()
    patch_resp = await api_client.patch(
        f"/api/v1/employees/{e['id']}", json={"notes": "raise pending"}
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["version"] == e["version"] + 1
    assert patch_resp.json()["notes"] == "raise pending"


@pytest.mark.asyncio
async def test_update_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.patch(
        f"/api/v1/employees/{missing}", json={"notes": "x"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_stale_if_match_412(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    resp = await api_client.patch(
        f"/api/v1/employees/{eid}",
        json={"notes": "stale"},
        headers={"If-Match": "999"},
    )
    assert resp.status_code == 412, resp.text


@pytest.mark.asyncio
async def test_update_malformed_if_match_400(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    resp = await api_client.patch(
        f"/api/v1/employees/{eid}",
        json={"notes": "x"},
        headers={"If-Match": "not-int"},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Terminate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_sets_end_date_and_reason(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    resp = await api_client.post(
        f"/api/v1/employees/{eid}/terminate",
        json={"end_date": "2026-06-30", "reason": "V"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["end_date"] == "2026-06-30"
    assert out["termination_reason"] == "V"


@pytest.mark.asyncio
async def test_terminate_twice_409(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    first = await api_client.post(
        f"/api/v1/employees/{eid}/terminate",
        json={"end_date": "2026-06-30", "reason": "V"},
    )
    assert first.status_code == 200, first.text
    second = await api_client.post(
        f"/api/v1/employees/{eid}/terminate",
        json={"end_date": "2026-07-31", "reason": "V"},
    )
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# TFN reveal endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tfn_reveal_404_when_absent(api_client: AsyncClient) -> None:
    body = await _body()  # no TFN
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    resp = await api_client.get(f"/api/v1/employees/{eid}/tfn")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_tfn_reveal_returns_plaintext(api_client: AsyncClient) -> None:
    body = await _body(contact_name="Pytest TFN reveal", tfn="876543210")
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    resp = await api_client.get(f"/api/v1/employees/{eid}/tfn")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["employee_id"] == eid
    assert out["tfn"] == "876543210"


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_removes_from_active_list(api_client: AsyncClient) -> None:
    body = await _body()
    create_resp = await api_client.post("/api/v1/employees", json=body)
    eid = create_resp.json()["id"]
    del_resp = await api_client.delete(f"/api/v1/employees/{eid}")
    assert del_resp.status_code == 204, del_resp.text
    list_resp = await api_client.get("/api/v1/employees")  # only_active=True default
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_archive_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.delete(f"/api/v1/employees/{missing}")
    assert resp.status_code == 404, resp.text
