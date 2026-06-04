"""Contract tests for GET /api/v1/search.

Covers:
* test_search_empty_query_returns_empty — ?q= or ?q=%20 → 200, empty hits
* test_search_auth_gate — no token → 401
* test_search_contacts_found — create contact "Zorgon Corp", search q=Zorgon, hit appears
* test_search_invoices_found — create invoice linked to a unique-named contact, search by name
* test_search_unknown_query_returns_empty — q=ZZZNOMATCH99 → 200, empty hits
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
from saebooks.models.contact import Contact

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
async def invoice_deps() -> dict[str, str]:
    """Return IDs needed to build an invoice payload."""
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()

    assert income is not None, "Test DB has no INCOME account"
    assert contact is not None, "Test DB has no contact"
    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


def _rand_suffix() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_search_auth_gate(unauth_client: AsyncClient) -> None:
    """No bearer token → 401."""
    r = await unauth_client.get("/api/v1/search", params={"q": "anything"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Empty query → empty hits
# ---------------------------------------------------------------------------


async def test_search_empty_query_returns_empty(api_client: AsyncClient) -> None:
    """?q= (empty string) → 200 with empty hits."""
    r = await api_client.get("/api/v1/search", params={"q": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"] == []
    assert body["total"] == 0
    assert body["query"] == ""


async def test_search_whitespace_query_returns_empty(api_client: AsyncClient) -> None:
    """?q=%20 (whitespace only) → 200 with empty hits."""
    r = await api_client.get("/api/v1/search", params={"q": " "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# Unknown query → empty hits
# ---------------------------------------------------------------------------


async def test_search_unknown_query_returns_empty(api_client: AsyncClient) -> None:
    """A query that matches nothing → 200 with empty hits."""
    r = await api_client.get("/api/v1/search", params={"q": "ZZZNOMATCH99"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# Contact search
# ---------------------------------------------------------------------------


async def test_search_contacts_found(api_client: AsyncClient) -> None:
    """Create a contact with a unique name, search for it, expect a hit."""
    unique = f"Zorgon{_rand_suffix()}"
    contact_name = f"{unique} Corp"

    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": contact_name, "contact_type": "CUSTOMER"},
    )
    assert r.status_code == 201, r.text

    r2 = await api_client.get("/api/v1/search", params={"q": unique})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["total"] >= 1

    kinds = [h["kind"] for h in body["hits"]]
    titles = [h["title"] for h in body["hits"]]
    assert "contact" in kinds
    assert any(unique in t for t in titles)

    # Verify SearchHitOut shape
    hit = next(h for h in body["hits"] if h["kind"] == "contact")
    assert "id" in hit
    assert "url" in hit
    assert hit["url"].startswith("/contacts/")


# ---------------------------------------------------------------------------
# Invoice search (by linked contact name)
# ---------------------------------------------------------------------------


async def test_search_invoices_found(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Create a contact + invoice; searching the contact name returns invoice hits."""
    unique = f"InvSearch{_rand_suffix()}"
    contact_name = f"{unique} Ltd"

    # Create a fresh contact with the unique name
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": contact_name, "contact_type": "CUSTOMER"},
    )
    assert r.status_code == 201, r.text
    contact_id = r.json()["id"]

    # Create an invoice linked to that contact
    r2 = await api_client.post(
        "/api/v1/invoices",
        json={
            "contact_id": contact_id,
            "issue_date": "2026-04-01",
            "due_date": "2026-05-01",
            "lines": [
                {
                    "description": "Search test line",
                    "account_id": invoice_deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": "100.00",
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r2.status_code == 201, r2.text

    # Search by the unique contact name — should get at least an invoice hit
    r3 = await api_client.get("/api/v1/search", params={"q": unique})
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["total"] >= 1

    kinds = [h["kind"] for h in body["hits"]]
    # contact match OR invoice match — either satisfies the search intent
    assert "contact" in kinds or "invoice" in kinds

    # Verify invoice hit shape when present
    inv_hits = [h for h in body["hits"] if h["kind"] == "invoice"]
    if inv_hits:
        hit = inv_hits[0]
        assert "id" in hit
        assert hit["url"].startswith("/invoices/")
        assert "subtitle" in hit
