"""B/48 — POST /api/v1/invoices/{id}/stripe-payment-link.

Tests
-----
1.  Happy path: posted invoice → 200 + payment_link URL + DB persisted.
2.  Draft invoice → 422 (not POSTED).
3.  Voided invoice → 422 (not POSTED).
4.  Invoice not found → 404.
5.  Stripe not configured (empty secret key) → 503.
6.  Stripe API returns 4xx → 502.
7.  Community edition (FLAG_STRIPE_INTEGRATION off) → 404.
8.  Business edition gate is open → 200.
9.  Zero balance invoice → 422.
10. payment_link URL is persisted to invoices.stripe_payment_link.

All Stripe HTTP calls are intercepted by respx.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.services.integrations.stripe_links import _CHECKOUT_URL

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STRIPE_SESSION_ID = "cs_test_b48_abc123"
_STRIPE_URL = "https://checkout.stripe.com/pay/cs_test_b48_abc123"
_FAKE_SECRET_KEY = "sk_test_fakekeyforb48testing"


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
def business_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set edition to business so FLAG_STRIPE_INTEGRATION is enabled."""
    monkeypatch.setattr(app_settings, "edition", "business")


@pytest.fixture
def stripe_key_configured(
    monkeypatch: pytest.MonkeyPatch, business_edition: None
) -> None:
    """Configure a fake Stripe secret key + business edition."""
    monkeypatch.setattr(app_settings, "stripe_secret_key", _FAKE_SECRET_KEY)


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
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert income is not None, "Test DB has no INCOME account in default tenant"
    assert contact is not None, "Test DB has no contact in default tenant"
    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


def _invoice_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": str(date.today()),
        "due_date": str(date.today()),
        "notes": "B/48 test invoice",
        "lines": [
            {
                "description": "Consulting services B48",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "200.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


def _stripe_session_response() -> httpx.Response:
    """Fake Stripe Checkout Session response body."""
    return httpx.Response(
        200,
        json={
            "id": _STRIPE_SESSION_ID,
            "object": "checkout.session",
            "url": _STRIPE_URL,
            "status": "open",
        },
    )


# ---------------------------------------------------------------------------
# Helpers — create + optionally post an invoice via the API.
# ---------------------------------------------------------------------------


async def _create_invoice(
    client: AsyncClient, deps: dict[str, str], **overrides: object
) -> dict:
    r = await client.post("/api/v1/invoices", json=_invoice_payload(deps, **overrides))
    assert r.status_code == 201, r.text
    return r.json()


async def _post_invoice(client: AsyncClient, inv: dict) -> dict:
    r = await client.post(
        f"/api/v1/invoices/{inv['id']}/post",
        headers={"If-Match": str(inv["version"])},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_happy_path_posted_invoice_returns_url(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """Posted invoice + Stripe configured → 200 with payment_link URL."""
    respx.post(_CHECKOUT_URL).mock(return_value=_stripe_session_response())

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payment_link"] == _STRIPE_URL


@respx.mock
async def test_payment_link_persisted_to_db(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """The URL is written to invoices.stripe_payment_link after success."""
    respx.post(_CHECKOUT_URL).mock(return_value=_stripe_session_response())

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )

    async with AsyncSessionLocal() as session:
        db_inv = await session.get(Invoice, uuid.UUID(posted["id"]))
        assert db_inv is not None
        assert db_inv.stripe_payment_link == _STRIPE_URL


async def test_draft_invoice_returns_422(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """DRAFT invoice → 422 (must be POSTED)."""
    inv = await _create_invoice(api_client, invoice_deps)

    r = await api_client.post(
        f"/api/v1/invoices/{inv['id']}/stripe-payment-link"
    )
    assert r.status_code == 422
    assert "POSTED" in r.json()["detail"]


async def test_voided_invoice_returns_422(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """VOIDED invoice → 422 (must be POSTED)."""
    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    # Void it.
    r_void = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/void",
        headers={"If-Match": str(posted["version"])},
    )
    assert r_void.status_code == 200, r_void.text

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 422
    assert "POSTED" in r.json()["detail"]


async def test_invoice_not_found_returns_404(
    api_client: AsyncClient,
    stripe_key_configured: None,
) -> None:
    """Unknown invoice ID → 404."""
    r = await api_client.post(
        f"/api/v1/invoices/{uuid.uuid4()}/stripe-payment-link"
    )
    assert r.status_code == 404


async def test_stripe_not_configured_returns_503(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    business_edition: None,
) -> None:
    """STRIPE_SECRET_KEY empty → 503 (configured but no key)."""
    monkeypatch.setattr(app_settings, "stripe_secret_key", "")

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


@respx.mock
async def test_stripe_api_error_returns_502(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """Stripe API returns 400 → 502 Bad Gateway."""
    respx.post(_CHECKOUT_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Invalid currency", "type": "invalid_request_error"}},
        )
    )

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 502
    assert "Stripe API error" in r.json()["detail"]


async def test_community_edition_returns_404(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Community edition doesn't have FLAG_STRIPE_INTEGRATION — 404."""
    monkeypatch.setattr(app_settings, "edition", "community")
    monkeypatch.setattr(app_settings, "stripe_secret_key", _FAKE_SECRET_KEY)

    # We still need a real invoice ID to call the endpoint.
    inv = await _create_invoice(api_client, invoice_deps)

    r = await api_client.post(
        f"/api/v1/invoices/{inv['id']}/stripe-payment-link"
    )
    assert r.status_code == 404


@respx.mock
async def test_business_edition_gate_is_open(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """Business edition has FLAG_STRIPE_INTEGRATION — endpoint is reachable."""
    respx.post(_CHECKOUT_URL).mock(return_value=_stripe_session_response())

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 200


@respx.mock
async def test_zero_balance_invoice_returns_422(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """Invoice where amount_paid >= total → 422 (no outstanding balance).

    We create and post a normal invoice, then manually set amount_paid = total
    in the DB to simulate a fully-paid invoice without going through the
    payment posting flow.
    """
    from decimal import Decimal as D
    from sqlalchemy import update as sa_update

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)
    inv_id = uuid.UUID(posted["id"])

    # Simulate full payment directly in the DB.
    async with AsyncSessionLocal() as session:
        db_inv = await session.get(Invoice, inv_id)
        assert db_inv is not None
        db_inv.amount_paid = db_inv.total
        await session.commit()

    r = await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )
    assert r.status_code == 422
    assert "balance" in r.json()["detail"].lower()


@respx.mock
async def test_stripe_request_contains_invoice_metadata(
    api_client: AsyncClient,
    invoice_deps: dict[str, str],
    stripe_key_configured: None,
) -> None:
    """The Stripe POST body must carry metadata[invoice_id]."""
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _stripe_session_response()

    respx.post(_CHECKOUT_URL).mock(side_effect=_capture)

    inv = await _create_invoice(api_client, invoice_deps)
    posted = await _post_invoice(api_client, inv)

    await api_client.post(
        f"/api/v1/invoices/{posted['id']}/stripe-payment-link"
    )

    assert len(captured) == 1
    body_text = captured[0].content.decode()
    assert f"metadata%5Binvoice_id%5D={posted['id']}" in body_text or \
           f"metadata[invoice_id]={posted['id']}" in body_text
