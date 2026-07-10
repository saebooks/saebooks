"""Contract tests for the pre-accounting MODULE app (#32 step 4).

These exercise the module surface directly (no engine facade in front): the
module runs the real ``services.quotes`` code in-process against the test DB
with tenant context taken from ``X-Tenant-Id`` / ``X-Company-Id`` headers.

Covers:
* token gate — 503 when ``PREACCOUNTING_TOKEN`` unset (fail-closed), 401 on a
  wrong/missing token, 200 with the right token.
* quote CRUD round-trip through the module (create → get → send → accept → list).
* one conversion through the module path (quote→invoice) asserting the
  two-phase fact-first hand-off completed and is not double-applied.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from preaccounting_app.main import app as module_app
from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

_TOKEN = "preacct-test-token"


@pytest.fixture
def module_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the module's inbound token; keep the base-url flag OFF so the
    module runs its service code in-process (never delegates to itself)."""
    monkeypatch.setattr(settings, "preaccounting_token", _TOKEN)
    monkeypatch.setattr(settings, "preaccounting_base_url", "")
    return _TOKEN


@pytest.fixture
async def deps() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.tenant_id == DEFAULT_TENANT_ID, Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert company is not None, "seed company missing"
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                    Account.company_id == company.id,
                ).limit(1)
            )
        ).scalars().first()
        assert income is not None, "no INCOME account in seed company"
        customer = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.company_id == company.id,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()
        if customer is None:
            customer = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Module Test Customer",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(customer)
            await session.commit()
            await session.refresh(customer)
    return {
        "company_id": str(company.id),
        "customer_id": str(customer.id),
        "income_account_id": str(income.id),
    }


def _client(headers: dict[str, str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=module_app), base_url="http://test", headers=headers
    )


def _auth_headers(deps: dict[str, str], token: str = _TOKEN) -> dict[str, str]:
    return {
        "X-PreAccounting-Token": token,
        "X-Tenant-Id": str(DEFAULT_TENANT_ID),
        "X-Company-Id": deps["company_id"],
    }


# --------------------------------------------------------------------------- #
# Token gate                                                                    #
# --------------------------------------------------------------------------- #
async def test_module_503_when_token_unconfigured(
    monkeypatch: pytest.MonkeyPatch, deps: dict[str, str]
) -> None:
    monkeypatch.setattr(settings, "preaccounting_token", "")
    monkeypatch.setattr(settings, "preaccounting_base_url", "")
    async with _client(_auth_headers(deps, token="anything")) as ac:
        r = await ac.post("/module/preaccounting/quotes/get", json={"quote_id": str(uuid.uuid4())})
    assert r.status_code == 503


async def test_module_401_on_wrong_token(module_token: str, deps: dict[str, str]) -> None:
    async with _client(_auth_headers(deps, token="wrong-token")) as ac:
        r = await ac.post("/module/preaccounting/quotes/get", json={"quote_id": str(uuid.uuid4())})
    assert r.status_code == 401


async def test_module_401_on_missing_token(module_token: str, deps: dict[str, str]) -> None:
    headers = _auth_headers(deps)
    headers.pop("X-PreAccounting-Token")
    async with _client(headers) as ac:
        r = await ac.post("/module/preaccounting/quotes/get", json={"quote_id": str(uuid.uuid4())})
    assert r.status_code == 401


async def test_healthz_is_open() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=module_app), base_url="http://test"
    ) as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "preaccounting"


# --------------------------------------------------------------------------- #
# Quote CRUD round-trip through the module                                      #
# --------------------------------------------------------------------------- #
def _create_body(deps: dict[str, str]) -> dict:
    return {
        "actor": "test:module",
        "customer_id": deps["customer_id"],
        "issue_date": "2026-05-01",
        "expiry_date": "2026-05-29",
        "notes": "module round-trip",
        "lines": [
            {
                "description": "Design",
                "quantity": "10",
                "unit_price": "150.00",
                "account_id": deps["income_account_id"],
            }
        ],
    }


async def test_quote_crud_round_trip(module_token: str, deps: dict[str, str]) -> None:
    async with _client(_auth_headers(deps)) as ac:
        r = await ac.post("/module/preaccounting/quotes/create", json=_create_body(deps))
        assert r.status_code == 201, r.text
        q = r.json()
        assert q["status"] == "DRAFT"
        assert q["version"] == 1
        assert len(q["lines"]) == 1
        qid = q["id"]

        r = await ac.post("/module/preaccounting/quotes/get", json={"quote_id": qid})
        assert r.status_code == 200
        assert r.json()["id"] == qid

        r = await ac.post(
            "/module/preaccounting/quotes/send",
            json={"quote_id": qid, "actor": "test:module", "expected_version": 1},
        )
        assert r.status_code == 200, r.text
        sent = r.json()
        assert sent["status"] == "SENT"
        assert sent["number"], "send should mint a quote number"

        r = await ac.post(
            "/module/preaccounting/quotes/accept",
            json={"quote_id": qid, "actor": "test:module", "expected_version": 2},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ACCEPTED"

        r = await ac.post("/module/preaccounting/quotes/list", json={"status": "ACCEPTED"})
        assert r.status_code == 200
        assert any(item["id"] == qid for item in r.json()["items"])


async def test_quote_send_stale_version_conflict(
    module_token: str, deps: dict[str, str]
) -> None:
    async with _client(_auth_headers(deps)) as ac:
        r = await ac.post("/module/preaccounting/quotes/create", json=_create_body(deps))
        qid = r.json()["id"]
        # expected_version 99 is stale → 409 with current state in body.
        r = await ac.post(
            "/module/preaccounting/quotes/send",
            json={"quote_id": qid, "actor": "test:module", "expected_version": 99},
        )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == qid


# --------------------------------------------------------------------------- #
# Conversion through the module (two-phase, fact-first)                         #
# --------------------------------------------------------------------------- #
async def test_convert_to_invoice_through_module(
    module_token: str, deps: dict[str, str]
) -> None:
    async with _client(_auth_headers(deps)) as ac:
        r = await ac.post("/module/preaccounting/quotes/create", json=_create_body(deps))
        qid = r.json()["id"]
        await ac.post(
            "/module/preaccounting/quotes/send",
            json={"quote_id": qid, "actor": "t", "expected_version": 1},
        )
        await ac.post(
            "/module/preaccounting/quotes/accept",
            json={"quote_id": qid, "actor": "t", "expected_version": 2},
        )

        r = await ac.post(
            "/module/preaccounting/quotes/convert-to-invoice",
            json={"quote_id": qid, "actor": "t", "expected_version": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        invoice_id = body["invoice_id"]
        assert invoice_id
        assert body["quote"]["status"] == "INVOICED"
        assert body["quote"]["invoice_id"] == invoice_id

        # Two-phase state committed: the quote reads back INVOICED with the
        # back-link stamped.
        r = await ac.post("/module/preaccounting/quotes/get", json={"quote_id": qid})
        assert r.json()["invoice_id"] == invoice_id

        # Idempotent hand-off: re-converting an already-INVOICED quote is
        # rejected (no second invoice minted), not silently double-applied.
        r = await ac.post(
            "/module/preaccounting/quotes/convert-to-invoice",
            json={"quote_id": qid, "actor": "t", "expected_version": 4},
        )
        assert r.status_code in (409, 422)
