"""Tests for the Stripe webhook at /webhooks/stripe.

Coverage:
* Valid Stripe-Signature accepted → 200
* Invalid signature rejected → 400
* Missing signature → 400
* Missing STRIPE_WEBHOOK_SECRET → 503
* customer.subscription.updated — sets tenant edition
* customer.subscription.deleted — reverts to community, clears sub_id
* URL is /webhooks/stripe NOT /api/v1/webhooks/stripe
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_TEST_TRUSTED_USER_HEADER", "1")

from saebooks.main import app  # noqa: E402

_TEST_WEBHOOK_SECRET = "whsec_test_stripe_cat_c_local_only"
_WEBHOOK_URL = "/webhooks/stripe"


def _sign(payload: bytes, secret: str = _TEST_WEBHOOK_SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.fixture(autouse=True)
def _set_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "stripe_webhook_secret", _TEST_WEBHOOK_SECRET)


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# URL location verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_url_is_not_under_api_v1(client: AsyncClient) -> None:
    """Verify the webhook is mounted at /webhooks/stripe NOT /api/v1/webhooks/stripe."""
    payload = json.dumps({"type": "ping"}).encode()
    sig = _sign(payload)

    # The new location should be reachable.
    resp_new = await client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": sig},
    )
    assert resp_new.status_code == 200, f"Expected 200 at /webhooks/stripe, got {resp_new.status_code}"

    # /api/v1/webhooks/stripe must NOT exist (rollup hasn't removed it yet
    # but it was never there in the new router).
    resp_old = await client.post(
        "/api/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": sig},
    )
    # 404 or 405 are both acceptable — the path doesn't exist in api_v1_router.
    assert resp_old.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_accepted(client: AsyncClient) -> None:
    """Stripe-Signature matching STRIPE_WEBHOOK_SECRET → 200."""
    payload = json.dumps({"type": "account.updated", "id": "evt_test1"}).encode()
    sig = _sign(payload)

    resp = await client.post(
        _WEBHOOK_URL,
        content=payload,
        headers={"Stripe-Signature": sig},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True


@pytest.mark.asyncio
async def test_invalid_signature_rejected(client: AsyncClient) -> None:
    """Bad HMAC → 400."""
    payload = json.dumps({"type": "ping"}).encode()
    ts = int(time.time())
    bad_sig = f"t={ts},v1=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    resp = await client.post(
        _WEBHOOK_URL,
        content=payload,
        headers={"Stripe-Signature": bad_sig},
    )
    assert resp.status_code == 400
    assert "Signature" in resp.json()["error"]


@pytest.mark.asyncio
async def test_missing_signature_header_rejected(client: AsyncClient) -> None:
    """Missing Stripe-Signature header → 400."""
    payload = json.dumps({"type": "ping"}).encode()
    resp = await client.post(_WEBHOOK_URL, content=payload)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_503_when_secret_not_configured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRIPE_WEBHOOK_SECRET unset → 503."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "stripe_webhook_secret", "")

    payload = json.dumps({"type": "ping"}).encode()
    resp = await client.post(
        _WEBHOOK_URL,
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=abc"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_stale_timestamp_rejected(client: AsyncClient) -> None:
    """Signature timestamp > 300s in the past → 400."""
    payload = json.dumps({"type": "ping"}).encode()
    old_ts = int(time.time()) - 400  # 400 seconds ago
    old_sig = _sign(payload, ts=old_ts)

    resp = await client.post(
        _WEBHOOK_URL,
        content=payload,
        headers={"Stripe-Signature": old_sig},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Subscription event processing
# ---------------------------------------------------------------------------


def _subscription_event(
    event_type: str,
    sub_id: str,
    edition: str = "pro",
    status: str = "active",
) -> bytes:
    return json.dumps({
        "id": f"evt_{sub_id}",
        "type": event_type,
        "data": {
            "object": {
                "id": sub_id,
                "status": status,
                "metadata": {"sae_edition": edition},
            }
        },
    }).encode()


@pytest.mark.asyncio
async def test_subscription_updated_active_sets_edition(
    client: AsyncClient,
) -> None:
    """customer.subscription.updated with active status → tenant edition updated."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    payload = _subscription_event("customer.subscription.updated", sub_id, edition="pro", status="active")
    sig = _sign(payload)

    mock_tenant = MagicMock()
    mock_tenant.id = uuid.uuid4()
    mock_tenant.edition = "community"

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_tenant
        return result

    with patch("saebooks.api.webhooks.stripe.AsyncSessionLocal") as mock_cm:
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session.commit = AsyncMock()
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _WEBHOOK_URL,
            content=payload,
            headers={"Stripe-Signature": sig},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["handled"] is True
    assert mock_tenant.edition == "pro"


@pytest.mark.asyncio
async def test_subscription_updated_lapsed_reverts_to_community(
    client: AsyncClient,
) -> None:
    """customer.subscription.updated with canceled → revert to community."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    payload = _subscription_event(
        "customer.subscription.updated", sub_id, edition="pro", status="canceled"
    )
    sig = _sign(payload)

    mock_tenant = MagicMock()
    mock_tenant.id = uuid.uuid4()
    mock_tenant.edition = "pro"

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_tenant
        return result

    with patch("saebooks.api.webhooks.stripe.AsyncSessionLocal") as mock_cm:
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session.commit = AsyncMock()
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _WEBHOOK_URL,
            content=payload,
            headers={"Stripe-Signature": sig},
        )

    assert resp.status_code == 200
    assert resp.json()["handled"] is True
    assert mock_tenant.edition == "community"


@pytest.mark.asyncio
async def test_subscription_deleted_clears_subscription_id(
    client: AsyncClient,
) -> None:
    """customer.subscription.deleted → edition=community + stripe_subscription_id cleared."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    payload = _subscription_event("customer.subscription.deleted", sub_id)
    sig = _sign(payload)

    mock_tenant = MagicMock()
    mock_tenant.id = uuid.uuid4()
    mock_tenant.edition = "business"
    mock_tenant.stripe_subscription_id = sub_id

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_tenant
        return result

    with patch("saebooks.api.webhooks.stripe.AsyncSessionLocal") as mock_cm:
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session.commit = AsyncMock()
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _WEBHOOK_URL,
            content=payload,
            headers={"Stripe-Signature": sig},
        )

    assert resp.status_code == 200
    assert resp.json()["handled"] is True
    assert mock_tenant.edition == "community"
    assert mock_tenant.stripe_subscription_id is None


@pytest.mark.asyncio
async def test_subscription_event_unlinked_tenant_handled_false(
    client: AsyncClient,
) -> None:
    """subscription event for unknown sub_id → handled=False (no tenant matched)."""
    sub_id = "sub_not_in_db"
    payload = _subscription_event("customer.subscription.updated", sub_id)
    sig = _sign(payload)

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None  # no match
        return result

    with patch("saebooks.api.webhooks.stripe.AsyncSessionLocal") as mock_cm:
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session.commit = AsyncMock()
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _WEBHOOK_URL,
            content=payload,
            headers={"Stripe-Signature": sig},
        )

    assert resp.status_code == 200
    assert resp.json()["handled"] is False


@pytest.mark.asyncio
async def test_unknown_event_type_ack_d_not_handled(client: AsyncClient) -> None:
    """Unknown event type → 200 received=True handled=False (Stripe retries avoided)."""
    payload = json.dumps({"type": "some.future.event", "id": "evt_future"}).encode()
    sig = _sign(payload)

    resp = await client.post(
        _WEBHOOK_URL,
        content=payload,
        headers={"Stripe-Signature": sig},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["handled"] is False
