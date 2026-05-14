"""Contract tests for /api/v1/quotes.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/quotes → 200 with pagination shape
* GET /api/v1/quotes/{id} → 200 with lines; 404 on missing UUID
* POST /api/v1/quotes → 201, version==1, change_log row created
* POST with X-Idempotency-Key → idempotent (same key, same body → same ID)
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE → 204 (hard delete)
* POST /{id}/send → DRAFT → SENT, mints quote number
* POST /{id}/accept → SENT → ACCEPTED, stamps accepted_at
* POST /{id}/decline → SENT → DECLINED, stamps declined_at
* POST /{id}/archive → any non-INVOICED → ARCHIVED
* Invalid state transitions rejected (e.g. accept a DRAFT)
* POST /{id}/convert-to-invoice → ACCEPTED → INVOICED, invoice created
* convert-to-invoice carries lines to new invoice
* Tenant isolation: tenant A cannot see tenant B's quotes
* Expiry-date filtering on list
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tenant import Tenant
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
async def quote_deps() -> dict[str, str]:
    """Return IDs needed to build a quote payload.

    Provisions a CUSTOMER contact and an INCOME account in the seed
    company / default tenant if they don't already exist.
    """
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

        customer = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()

        if customer is None:
            company = (
                await session.execute(
                    select(Company).where(
                        Company.tenant_id == DEFAULT_TENANT_ID,
                        Company.archived_at.is_(None),
                    ).limit(1)
                )
            ).scalars().first()
            assert company is not None, "Seed company missing"
            customer = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Test Customer",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(customer)
            await session.commit()
            await session.refresh(customer)

    assert income is not None, "Test DB has no INCOME account in default tenant"
    assert customer is not None, "Failed to provision test customer"
    return {
        "income_account_id": str(income.id),
        "customer_id": str(customer.id),
    }


def _quote_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "customer_id": deps["customer_id"],
        "issue_date": "2026-05-01",
        "expiry_date": "2026-05-29",
        "notes": "Test quote",
        "lines": [
            {
                "description": "Engineering services — design",
                "quantity": "10",
                "unit_price": "150.00",
                "account_id": deps["income_account_id"],
            },
            {
                "description": "Engineering services — installation",
                "quantity": "5",
                "unit_price": "200.00",
                "account_id": deps["income_account_id"],
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_quotes_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/quotes")
    assert r.status_code == 401


async def test_quotes_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/quotes")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_quotes_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/quotes")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_quotes_list_filter_by_status(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/quotes", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


async def test_quotes_list_filter_by_expiry(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Expiry-date filter: only quotes expiring on/before the date appear."""
    r = await api_client.post(
        "/api/v1/quotes",
        json=_quote_payload(
            quote_deps, expiry_date="2026-05-15"
        ),
    )
    assert r.status_code == 201
    quote_id = r.json()["id"]

    # Filter to before the expiry
    r2 = await api_client.get(
        "/api/v1/quotes", params={"expiry_before": "2026-05-20"}
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert quote_id in ids

    # Filter past the expiry — should not appear
    r3 = await api_client.get(
        "/api/v1/quotes", params={"expiry_before": "2026-05-10"}
    )
    assert r3.status_code == 200
    ids3 = [i["id"] for i in r3.json()["items"]]
    assert quote_id not in ids3


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_quotes_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/quotes/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_quotes_get_200_with_lines(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201, r.text
    quote_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/quotes/{quote_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == quote_id
    assert len(body["lines"]) == 2
    assert body["lines"][0]["description"] == "Engineering services — design"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_quotes_create_201(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["status"] == "DRAFT"
    assert body["number"] is None  # minted on send
    assert "tenant_id" in body
    assert len(body["lines"]) == 2
    # subtotal = 10*150 + 5*200 = 2500
    assert float(body["subtotal"]) == 2500.0


async def test_quotes_create_change_log(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog).where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(quote_id),
                    ChangeLog.entity == "quote",
                ).order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1


async def test_quotes_create_idempotent(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Same X-Idempotency-Key + same body → same quote ID returned."""
    idem_key = str(uuid.uuid4())
    headers = {"X-Idempotency-Key": idem_key}
    payload = _quote_payload(quote_deps)

    r1 = await api_client.post("/api/v1/quotes", json=payload, headers=headers)
    assert r1.status_code == 201, r1.text
    id1 = r1.json()["id"]

    r2 = await api_client.post("/api/v1/quotes", json=payload, headers=headers)
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    assert id1 == id2


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_quotes_update_bumps_version(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201, r.text
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/quotes/{quote_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["notes"] == "Updated notes"


async def test_quotes_update_requires_if_match(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/quotes/{quote_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


async def test_quotes_stale_if_match_returns_409(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/quotes/{quote_id}",
        json={"notes": "stale"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == quote_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (hard delete)
# ---------------------------------------------------------------------------


async def test_quotes_delete_204(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/quotes/{quote_id}")
    assert r2.status_code == 204

    r3 = await api_client.get(f"/api/v1/quotes/{quote_id}")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# Send (DRAFT → SENT)
# ---------------------------------------------------------------------------


async def test_quotes_send_mints_number_and_flips_sent(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    sent = r2.json()
    assert sent["status"] == "SENT"
    assert sent["number"] is not None
    assert len(sent["number"]) > 0
    assert sent["version"] == v + 1


async def test_quotes_send_invalid_state_rejected(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Sending an already-SENT quote must fail with 422."""
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v2 = r2.json()["version"]

    # Try to send again
    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v2)}
    )
    assert r3.status_code == 422


# ---------------------------------------------------------------------------
# Accept (SENT → ACCEPTED)
# ---------------------------------------------------------------------------


async def test_quotes_accept_stamps_accepted_at(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    # Send first
    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["status"] == "ACCEPTED"
    assert body["accepted_at"] is not None
    assert body["version"] == v + 1


async def test_quotes_accept_requires_sent_state(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Accepting a DRAFT quote must be rejected."""
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Decline (SENT → DECLINED)
# ---------------------------------------------------------------------------


async def test_quotes_decline_stamps_declined_at(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/decline", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["status"] == "DECLINED"
    assert body["declined_at"] is not None


async def test_quotes_decline_requires_sent_state(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/decline", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


async def test_quotes_archive_from_draft(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/archive", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "ARCHIVED"


async def test_quotes_archive_from_sent(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/archive", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "ARCHIVED"


async def test_quotes_archive_invoiced_rejected(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Cannot archive an INVOICED quote."""
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    # Walk to INVOICED
    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    v = r2.json()["version"]
    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    v = r3.json()["version"]
    r4 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice",
        headers={"If-Match": str(v)},
    )
    assert r4.status_code == 200
    v = r4.json()["quote"]["version"]

    r5 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/archive", headers={"If-Match": str(v)}
    )
    assert r5.status_code == 422


# ---------------------------------------------------------------------------
# Convert-to-invoice
# ---------------------------------------------------------------------------


async def test_quotes_convert_to_invoice_creates_invoice(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200
    v = r3.json()["version"]

    r4 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice",
        headers={"If-Match": str(v)},
    )
    assert r4.status_code == 200, r4.text
    body = r4.json()
    assert body["quote"]["status"] == "INVOICED"
    assert body["quote"]["invoiced_at"] is not None
    assert body["quote"]["invoice_id"] is not None
    assert "invoice_id" in body

    # Invoice is fetchable & in DRAFT
    invoice_id = body["invoice_id"]
    r5 = await api_client.get(f"/api/v1/invoices/{invoice_id}")
    assert r5.status_code == 200
    inv = r5.json()
    assert inv["status"] == "DRAFT"
    assert inv["contact_id"] == quote_deps["customer_id"]


async def test_quotes_convert_carries_lines_to_invoice(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Lines with account_id carry over to the new invoice."""
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    v = r2.json()["version"]
    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    v = r3.json()["version"]
    r4 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice",
        headers={"If-Match": str(v)},
    )
    assert r4.status_code == 200
    invoice_id = r4.json()["invoice_id"]

    r5 = await api_client.get(f"/api/v1/invoices/{invoice_id}")
    inv = r5.json()
    # Both lines had account_id so both should carry over
    assert len(inv["lines"]) == 2
    descriptions = {ln["description"] for ln in inv["lines"]}
    assert "Engineering services — design" in descriptions
    assert "Engineering services — installation" in descriptions


async def test_quotes_convert_requires_accepted_state(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """Convert-to-invoice must reject a SENT quote."""
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice",
        headers={"If-Match": str(v)},
    )
    assert r3.status_code == 422


async def test_quotes_convert_hard_fails_on_missing_account_id(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    """convert-to-invoice must 422 when any line is missing account_id,
    and the error body must identify the offending line number(s)."""
    # Create a quote where line 2 has no account_id
    payload = _quote_payload(
        quote_deps,
        lines=[
            {
                "description": "Line with account",
                "quantity": "1",
                "unit_price": "100.00",
                "account_id": quote_deps["income_account_id"],
            },
            {
                "description": "Line without account",
                "quantity": "2",
                "unit_price": "50.00",
                # no account_id
            },
        ],
    )
    r = await api_client.post("/api/v1/quotes", json=payload)
    assert r.status_code == 201, r.text
    quote_id = r.json()["id"]
    v = r.json()["version"]

    # Walk to ACCEPTED
    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200, r2.text
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200, r3.text
    v = r3.json()["version"]

    # Convert should hard-fail with 422
    r4 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice",
        headers={"If-Match": str(v)},
    )
    assert r4.status_code == 422, r4.text

    # Error body must mention the missing line number
    detail = r4.json().get("detail", "")
    assert "2" in detail, f"Expected line number '2' in error detail, got: {detail}"
    assert "account_id" in detail.lower(), f"Expected 'account_id' in error detail, got: {detail}"


# ---------------------------------------------------------------------------
# Tenant isolation (RLS)
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_tenant_quotes_seed() -> dict:
    """Create two separate tenants each with a company + customer contact.

    Returns dict with 'alpha' and 'beta' keys, each containing
    tenant_id, company_id, customer_id, income_account_id.
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict = {}

    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()
            customer_id = uuid.uuid4()
            account_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"QT-{label}-{suffix}",
                    slug=f"qt-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"QT-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()

            session.add(
                Contact(
                    id=customer_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    name=f"QT-Customer-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"QT{suffix[:3]}{label[0].upper()}",
                    name=f"QT Income {label}",
                    account_type=AccountType.INCOME,
                )
            )
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "customer_id": customer_id,
                "income_account_id": account_id,
            }

        await session.commit()

    yield out

    # Cleanup
    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            ids = out[label]
            await session.execute(
                text(
                    "DELETE FROM quote_lines WHERE quote_id IN "
                    "(SELECT id FROM quotes WHERE company_id = :cid)"
                ),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM quotes WHERE company_id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM contacts WHERE id = :id"),
                {"id": ids["customer_id"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE id = :id"),
                {"id": ids["income_account_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": ids["tenant_id"]},
            )
        await session.commit()


async def test_quotes_tenant_isolation(
    two_tenant_quotes_seed: dict,
) -> None:
    """Tenant A cannot see tenant B's quotes via the service layer."""
    from saebooks.services import quotes as svc
    from saebooks.models.quote import QuoteStatus
    from datetime import date

    alpha = two_tenant_quotes_seed["alpha"]
    beta = two_tenant_quotes_seed["beta"]

    # Create a quote in beta tenant
    async with AsyncSessionLocal() as session:
        beta_quote = await svc.api_create(
            session,
            beta["company_id"],
            beta["tenant_id"],
            actor="test",
            customer_id=beta["customer_id"],
            issue_date=date(2026, 5, 1),
        )

    beta_quote_id = beta_quote.id

    # Alpha tenant cannot fetch beta's quote
    async with AsyncSessionLocal() as session:
        found = await svc.api_get(
            session, beta_quote_id, tenant_id=alpha["tenant_id"]
        )
    assert found is None, "Cross-tenant leak: alpha can see beta's quote"

    # Alpha tenant list does not include beta's quote
    async with AsyncSessionLocal() as session:
        rows, _ = await svc.list_active(
            session,
            alpha["company_id"],
            alpha["tenant_id"],
        )
    ids = [q.id for q in rows]
    assert beta_quote_id not in ids, "Cross-tenant leak: beta's quote appears in alpha list"


# ---------------------------------------------------------------------------
# change_log audit trail: create + send + archive
# ---------------------------------------------------------------------------


async def test_quotes_change_log_sequence(
    api_client: AsyncClient, quote_deps: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/quotes", json=_quote_payload(quote_deps))
    assert r.status_code == 201
    quote_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/quotes/{quote_id}/archive", headers={"If-Match": str(v)}
    )
    assert r3.status_code == 200

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog).where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(quote_id),
                    ChangeLog.entity == "quote",
                ).order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
