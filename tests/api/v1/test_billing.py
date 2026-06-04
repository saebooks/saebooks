"""Phase 3 — Stripe billing webhook tests.

We don't hit the real Stripe API (the create_checkout_session unit
test is covered by smoke testing). What we do test:

* Webhook with no Stripe-Signature → 400
* Webhook with bad signature → 400
* checkout.session.completed → tenant edition + Stripe IDs persisted
* customer.subscription.deleted → edition reverts to community,
  stripe_subscription_id cleared
* checkout.session.completed for an UNKNOWN email → mints fresh
  tenant + user with magic-link
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, hash_password

_TEST_WEBHOOK_SECRET = "whsec_test_local_only_8675309"


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    os.environ["SAEBOOKS_SECRET_KEY"] = "test-secret-billing"
    os.environ["STRIPE_WEBHOOK_SECRET"] = _TEST_WEBHOOK_SECRET
    # Force config object to pick it up
    from saebooks.config import settings as _settings

    object.__setattr__(_settings, "stripe_webhook_secret", _TEST_WEBHOOK_SECRET)
    _reset_secret_cache()
    yield
    _reset_secret_cache()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _sign(payload: bytes, secret: str = _TEST_WEBHOOK_SECRET, ts: int | None = None) -> str:
    """Build a Stripe-Signature header for ``payload``."""
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.mark.asyncio
async def test_webhook_missing_signature_400(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/billing/webhook", content=b"{}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_bad_signature_400(client: AsyncClient) -> None:
    payload = b'{"type":"ping"}'
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_checkout_completed_existing_user_attaches_to_tenant(
    client: AsyncClient,
) -> None:
    email = f"billing-{uuid.uuid4().hex[:8]}@example.test"

    # Seed: tenant + verified user
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.email == email))
        session.add(
            Tenant(
                id=tenant_id,
                name=email.split("@", 1)[0],
                slug=f"existing-{uuid.uuid4().hex[:6]}",
                edition="community",
            )
        )
        await session.flush()
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=email,
                email=email,
                role="owner",
                password_hash=hash_password("test-pass-123"),
                email_verified_at=datetime.now(UTC),
                version=1,
            )
        )
        await session.commit()

    sub_id = f"sub_{uuid.uuid4().hex[:24]}"
    cust_id = f"cus_{uuid.uuid4().hex[:14]}"
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{uuid.uuid4().hex}",
                "customer_email": email,
                "customer": cust_id,
                "subscription": sub_id,
                "metadata": {"sae_edition": "business"},
            }
        },
    }
    body = json.dumps(event).encode()
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=body,
        headers={"Stripe-Signature": _sign(body)},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant.edition == "business"
        assert tenant.stripe_customer_id == cust_id
        assert tenant.stripe_subscription_id == sub_id


@pytest.mark.asyncio
async def test_subscription_deleted_reverts_to_community(client: AsyncClient) -> None:
    sub_id = f"sub_{uuid.uuid4().hex[:24]}"
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="ToDelete",
                slug=f"todelete-{uuid.uuid4().hex[:6]}",
                edition="pro",
                stripe_subscription_id=sub_id,
                stripe_customer_id="cus_x",
            )
        )
        await session.commit()

    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": sub_id, "metadata": {}}},
    }
    body = json.dumps(event).encode()
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=body,
        headers={"Stripe-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    async with AsyncSessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant.edition == "community"
        assert tenant.stripe_subscription_id is None


@pytest.mark.asyncio
async def test_checkout_completed_unknown_email_mints_tenant(client: AsyncClient) -> None:
    email = f"newbuyer-{uuid.uuid4().hex[:8]}@example.test"
    sub_id = f"sub_{uuid.uuid4().hex[:24]}"
    cust_id = f"cus_{uuid.uuid4().hex[:14]}"

    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.commit()

    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{uuid.uuid4().hex}",
                "customer_email": email,
                "customer": cust_id,
                "subscription": sub_id,
                "metadata": {"sae_edition": "pro"},
            }
        },
    }
    body = json.dumps(event).encode()
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=body,
        headers={"Stripe-Signature": _sign(body)},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        assert user is not None
        assert user.role == "owner"
        assert user.email_verified_at is not None
        assert user.password_hash is None  # password set via magic-link
        assert user.magic_link_token_hash is not None

        tenant = await session.get(Tenant, user.tenant_id)
        assert tenant.edition == "pro"
        assert tenant.stripe_subscription_id == sub_id


# ---------------------------------------------------------------------------
# POST /billing/checkout-session — period passthrough (yearly support)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_session_passes_period_year(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout-session route must forward ``period`` from the
    request body into ``create_checkout_session(..., period=...)``.

    We don't hit Stripe — patch the underlying helper and assert it
    was called with ``period='year'``. Default-month covered by the
    next test.
    """
    from saebooks.services.jwt_tokens import make_access_token

    email = f"yearly-{uuid.uuid4().hex[:8]}@example.test"
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=email.split("@", 1)[0],
                slug=f"yearly-{uuid.uuid4().hex[:6]}",
                edition="community",
            )
        )
        await session.flush()
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            username=email,
            email=email,
            role="owner",
            password_hash=hash_password("test-pass-123"),
            email_verified_at=datetime.now(UTC),
            version=1,
            password_version=0,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    seen: dict[str, Any] = {}

    async def _fake_create(edition: str, customer_email: str, *, period: str = "month", **kw: Any) -> dict[str, str]:
        seen["edition"] = edition
        seen["email"] = customer_email
        seen["period"] = period
        return {
            "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_year",
            "session_id": "cs_test_year",
        }

    monkeypatch.setattr(
        "saebooks.api.v1.billing.create_checkout_session", _fake_create
    )

    resp = await client.post(
        "/api/v1/billing/checkout-session",
        json={"edition": "business", "period": "year"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["edition"] == "business"
    assert seen["email"] == email
    assert seen["period"] == "year"
    assert resp.json()["checkout_url"].startswith("https://checkout.stripe.com/")


@pytest.mark.asyncio
async def test_checkout_session_period_defaults_to_month(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body without an explicit ``period`` defaults to month — keeps
    pre-yearly callers working."""
    from saebooks.services.jwt_tokens import make_access_token

    email = f"monthly-{uuid.uuid4().hex[:8]}@example.test"
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=email.split("@", 1)[0],
                slug=f"monthly-{uuid.uuid4().hex[:6]}",
                edition="community",
            )
        )
        await session.flush()
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            username=email,
            email=email,
            role="owner",
            password_hash=hash_password("test-pass-123"),
            email_verified_at=datetime.now(UTC),
            version=1,
            password_version=0,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    seen: dict[str, Any] = {}

    async def _fake_create(edition: str, customer_email: str, *, period: str = "month", **kw: Any) -> dict[str, str]:
        seen["period"] = period
        return {
            "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_month",
            "session_id": "cs_test_month",
        }

    monkeypatch.setattr(
        "saebooks.api.v1.billing.create_checkout_session", _fake_create
    )

    resp = await client.post(
        "/api/v1/billing/checkout-session",
        json={"edition": "business"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["period"] == "month"


@pytest.mark.asyncio
async def test_checkout_session_rejects_unknown_period(
    client: AsyncClient,
) -> None:
    """Pydantic Literal validation kicks bad period values back as 422."""
    # No DB / JWT needed — validation happens before deps.
    # But we still need a bearer to get past require_bearer.
    from saebooks.services.jwt_tokens import make_access_token

    email = f"bad-period-{uuid.uuid4().hex[:8]}@example.test"
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=email.split("@", 1)[0],
                slug=f"badperiod-{uuid.uuid4().hex[:6]}",
                edition="community",
            )
        )
        await session.flush()
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            username=email,
            email=email,
            role="owner",
            password_hash=hash_password("test-pass-123"),
            email_verified_at=datetime.now(UTC),
            version=1,
            password_version=0,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    resp = await client.post(
        "/api/v1/billing/checkout-session",
        json={"edition": "business", "period": "weekly"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
