"""HTTP contract tests for GET /api/v1/tpar/{id}/lines.bde.

Drives a real TPAR run (seed company + TPAR-flagged supplier + posted bill →
build_tpar_run) and downloads the BDE flat file through the API. Seeding mirrors
the service-level test_tpar_run_renders_bde_file; this asserts the HTTP surface
(auth gate, 404, run-type toggle, valid FPAIVV03.0 bytes).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.jurisdictions.au import tpar as svc
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import bills as bill_svc
from saebooks.services import business_identifiers
from saebooks.services.companies import ensure_seed_company

pytestmark = pytest.mark.postgres_only

VALID_ABN = "51824753556"
RECORD_LENGTH = 996


@pytest.fixture
async def api_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {current_token()}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _seed_finalised_run() -> uuid.UUID:
    """Seed the active (seed) company with a TPAR-flagged supplier + posted bill,
    build a run in a far-future FY (isolated from other runs), return run_id."""
    fy_year = 2200 + secrets.randbelow(1500)
    fy_start, fy_end = date(fy_year, 7, 1), date(fy_year + 1, 6, 30)

    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        company = await session.get(Company, company.id)
        company.address = {
            "line1": "1 Example Street", "suburb": "Brisbane",
            "state": "QLD", "postcode": "4000",
        }
        company.phone = company.phone or "0733331234"
        await business_identifiers.upsert(
            session, company.id, "au_abn", VALID_ABN, tenant_id=company.tenant_id
        )
        name = f"BDE HTTP Subbie {secrets.token_hex(4)}"
        contact = Contact(
            company_id=company.id, name=name, family_name="Smith", given_name="Alex",
            contact_type=ContactType.SUPPLIER, abn=VALID_ABN, is_tpar_supplier=True,
            address_line1="2 Subbie Street", city="Cairns", state="QLD",
            postcode="4870", phone="0740001234", email="alex@example.com",
        )
        session.add(contact)
        await session.commit()
        cid, contact_id, tenant_id = company.id, contact.id, company.tenant_id
        expense = (
            await session.execute(
                select(Account).where(Account.company_id == cid, Account.code == "6-1000")
            )
        ).scalar_one()

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session, company_id=cid, contact_id=contact_id,
            issue_date=date(fy_year, 9, 1), due_date=date(fy_year, 9, 1),
            lines=[{
                "description": "subcontract work", "account_id": expense.id,
                "tax_code_id": None, "quantity": Decimal("1"),
                "unit_price": Decimal("5600.85"), "discount_pct": Decimal("0"),
            }],
        )
        await bill_svc.post_bill(session, bill.id, posted_by="tests")

    async with AsyncSessionLocal() as session:
        return await svc.build_tpar_run(
            session, tenant_id=tenant_id, company_id=cid, fy_start=fy_start, fy_end=fy_end
        )


def _records(body: bytes) -> list[str]:
    s = body.decode("ascii")
    assert len(s) % RECORD_LENGTH == 0, "file length not a multiple of 996"
    return [s[i : i + RECORD_LENGTH] for i in range(0, len(s), RECORD_LENGTH)]


async def test_download_bde_returns_valid_flat_file(api_client: AsyncClient):
    run_id = await _seed_finalised_run()
    r = await api_client.get(f"/api/v1/tpar/{run_id}/lines.bde")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert f"tpar-{run_id}.bde.txt" in r.headers["content-disposition"]
    recs = _records(r.content)
    assert recs[0][3:17] == "IDENTREGISTER1"
    assert recs[0][28] == "T"  # defaults to test-facility run type
    assert any(rec[3:9] == "DPAIVS" for rec in recs)
    assert recs[-1][3:13] == "FILE-TOTAL"


async def test_production_flag_flips_run_type(api_client: AsyncClient):
    run_id = await _seed_finalised_run()
    r = await api_client.get(f"/api/v1/tpar/{run_id}/lines.bde?production=true")
    assert r.status_code == 200
    assert _records(r.content)[0][28] == "P"


async def test_download_bde_requires_bearer(unauth_client: AsyncClient):
    r = await unauth_client.get(f"/api/v1/tpar/{uuid.uuid4()}/lines.bde")
    assert r.status_code == 401


async def test_unknown_run_is_404(api_client: AsyncClient):
    r = await api_client.get(f"/api/v1/tpar/{uuid.uuid4()}/lines.bde")
    assert r.status_code == 404
