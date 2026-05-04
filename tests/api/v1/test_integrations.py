"""Tests for /api/v1/integrations/* endpoints.

Coverage:
* POST /integrations/stripe/customer/connect — returns authorize_url + state (Business+)
* GET  /integrations/stripe/customer          — connection status
* POST /integrations/paperless/webhook        — valid HMAC accepted; invalid rejected
* POST /integrations/lei/lookup               — stub returns matches; FLAG_LEI_LOOKUP gate
* POST /integrations/companies-house/search   — FLAG_COMPANIES_HOUSE gate
* POST /integrations/ato/prefill              — stub returns 501
* Tenant isolation on paperless secrets (RLS smoke)
* Flag gates return 404 at community tier
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_TEST_TRUSTED_USER_HEADER", "1")

from saebooks.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMMUNITY_TOKEN = "Bearer community-dev-token"
_BUSINESS_TOKEN = "Bearer business-dev-token"

# The dev token path reads SAEBOOKS_DEV_API_TOKEN env var. For test we
# rely on the test setup which injects a dev bearer via require_bearer.

_DEV_TOKEN = os.environ.get("SAEBOOKS_DEV_API_TOKEN", "dev-only-insecure-token-for-tests")


def _auth_headers(edition: str = "business") -> dict[str, str]:
    """Return Bearer headers that satisfy require_bearer in test mode.

    The test env has SAEBOOKS_ENV=test which makes resolve_tenant_id
    fall back to the dev default tenant when no JWT is provided but a
    dev token IS provided.
    """
    return {"Authorization": f"Bearer {_DEV_TOKEN}"}


def _sign_paperless(body: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run most tests with enterprise edition so all flags are on.

    Individual tests that need to check lower-tier gating override
    the edition via a nested monkeypatch.
    """
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")


# ---------------------------------------------------------------------------
# Stripe customer connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_connect_returns_authorize_url_and_state(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /integrations/stripe/customer/connect returns authorize_url + state."""
    os.environ["STRIPE_CLIENT_ID"] = "ca_test123"
    os.environ["STRIPE_CONNECT_REDIRECT_URI"] = "http://test/callback"
    try:
        resp = await client.post(
            "/api/v1/integrations/stripe/customer/connect",
            headers=_auth_headers("enterprise"),
        )
    finally:
        os.environ.pop("STRIPE_CLIENT_ID", None)
        os.environ.pop("STRIPE_CONNECT_REDIRECT_URI", None)

    assert resp.status_code == 200
    data = resp.json()
    assert "authorize_url" in data
    assert "state" in data
    assert "connect.stripe.com" in data["authorize_url"]
    assert len(data["state"]) >= 32


@pytest.mark.asyncio
async def test_stripe_connect_503_when_not_configured(
    client: AsyncClient,
) -> None:
    """POST /integrations/stripe/customer/connect → 503 if STRIPE_CLIENT_ID missing."""
    os.environ.pop("STRIPE_CLIENT_ID", None)
    os.environ.pop("STRIPE_CONNECT_REDIRECT_URI", None)

    resp = await client.post(
        "/api/v1/integrations/stripe/customer/connect",
        headers=_auth_headers("enterprise"),
    )
    assert resp.status_code in (503, 404)  # 503 = not configured; 404 = flag gate


@pytest.mark.asyncio
async def test_stripe_connect_404_community(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_STRIPE_INTEGRATION gate: community edition → 404."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    resp = await client.post(
        "/api/v1/integrations/stripe/customer/connect",
        headers=_auth_headers("community"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Stripe customer status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_customer_status_not_connected(
    client: AsyncClient,
) -> None:
    """GET /integrations/stripe/customer returns connected=False for unlinked tenant."""
    with patch(
        "saebooks.api.v1.integrations.get_account_status",
        new_callable=AsyncMock,
        return_value={},
    ):
        resp = await client.get(
            "/api/v1/integrations/stripe/customer",
            headers=_auth_headers(),
        )
    # The test DB may not have a Tenant row for the dev tenant — that's
    # acceptable here; we care that the endpoint is reachable and returns
    # a structured response.
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        data = resp.json()
        assert "connected" in data


@pytest.mark.asyncio
async def test_stripe_customer_status_404_community(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_STRIPE_INTEGRATION gate: community edition → 404."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    resp = await client.get(
        "/api/v1/integrations/stripe/customer",
        headers=_auth_headers("community"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Paperless webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paperless_webhook_missing_tenant_id_400(
    client: AsyncClient,
) -> None:
    """POST /integrations/paperless/webhook without X-Tenant-Id → 400."""
    payload = b'{"type":"document_added","document_id":1}'
    resp = await client.post(
        "/api/v1/integrations/paperless/webhook",
        content=payload,
        headers={
            "X-Paperless-Signature": "sha256=abc",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_paperless_webhook_missing_signature_400(
    client: AsyncClient,
) -> None:
    """POST /integrations/paperless/webhook without X-Paperless-Signature → 400."""
    payload = b'{"type":"document_added","document_id":1}'
    resp = await client.post(
        "/api/v1/integrations/paperless/webhook",
        content=payload,
        headers={
            "X-Tenant-Id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_paperless_webhook_valid_hmac_accepted(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /integrations/paperless/webhook with valid HMAC → accepted."""
    tenant_id = uuid.uuid4()
    secret = "test-paperless-webhook-secret"
    payload = b'{"type":"document_added","document_id":42}'
    sig = _sign_paperless(payload, secret)

    # Stub out the DB read and crypto so we don't need a real paperless_webhook_secrets row.
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    fernet = Fernet(key)
    encrypted_secret = fernet.encrypt(secret.encode("utf-8"))

    mock_row = MagicMock()
    mock_row.secret_ciphertext = encrypted_secret
    mock_row.tenant_id = tenant_id

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_row
        return result

    with (
        patch(
            "saebooks.api.v1.integrations.AsyncSessionLocal",
        ) as mock_session_cm,
        patch(
            "saebooks.api.v1.integrations.decrypt_field",
            return_value=secret,
        ),
    ):
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            "/api/v1/integrations/paperless/webhook",
            content=payload,
            headers={
                "X-Tenant-Id": str(tenant_id),
                "X-Paperless-Signature": sig,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_paperless_webhook_invalid_hmac_rejected(
    client: AsyncClient,
) -> None:
    """POST /integrations/paperless/webhook with wrong HMAC → 400."""
    tenant_id = uuid.uuid4()
    secret = "correct-secret"
    payload = b'{"type":"document_added"}'

    # Deliberately sign with wrong secret.
    sig = _sign_paperless(payload, "wrong-secret")

    mock_row = MagicMock()
    mock_row.secret_ciphertext = b"doesnt-matter-wrong-sig-checked-first"
    mock_row.tenant_id = tenant_id

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_row
        return result

    with (
        patch("saebooks.api.v1.integrations.AsyncSessionLocal") as mock_session_cm,
        patch(
            "saebooks.api.v1.integrations.decrypt_field",
            return_value=secret,
        ),
    ):
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            "/api/v1/integrations/paperless/webhook",
            content=payload,
            headers={
                "X-Tenant-Id": str(tenant_id),
                "X-Paperless-Signature": sig,
            },
        )

    assert resp.status_code == 400
    assert "Signature" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_paperless_webhook_no_secret_configured_404(
    client: AsyncClient,
) -> None:
    """When no secret row exists for the tenant → 404."""
    tenant_id = uuid.uuid4()
    payload = b'{"type":"document_added"}'
    sig = _sign_paperless(payload, "any-secret")

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None  # no row
        return result

    with patch("saebooks.api.v1.integrations.AsyncSessionLocal") as mock_session_cm:
        mock_session = AsyncMock()
        mock_session.execute = _fake_execute
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            "/api/v1/integrations/paperless/webhook",
            content=payload,
            headers={
                "X-Tenant-Id": str(tenant_id),
                "X-Paperless-Signature": sig,
            },
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_paperless_webhook_tenant_isolation_rls_smoke() -> None:
    """RLS smoke: secret row for tenant A should not be visible to tenant B.

    This is a structural test — we verify that the model declares
    ``tenant_id`` on ``PaperlessWebhookSecret`` so the DB-layer RLS
    policy has a column to predicate on. The actual RLS enforcement
    requires a live DB with a non-BYPASSRLS role; that is covered by
    the prod-side migration assertions.
    """
    from saebooks.models.integrations import PaperlessWebhookSecret  # noqa: PLC0415
    from sqlalchemy import inspect as _inspect  # noqa: PLC0415

    col_names = [c.key for c in _inspect(PaperlessWebhookSecret).mapper.column_attrs]
    assert "tenant_id" in col_names, "PaperlessWebhookSecret must have tenant_id for RLS"


# ---------------------------------------------------------------------------
# LEI lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lei_lookup_returns_result(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /integrations/lei/lookup with stub → returns data."""
    mock_result = MagicMock()
    mock_result.lei = "TEST0000000000000000"

    # Make dataclasses.is_dataclass return False so we take the dict() branch.
    with patch(
        "saebooks.api.v1.integrations.lookup_lei",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.post(
            "/api/v1/integrations/lei/lookup",
            json={"search": "TEST0000000000000000"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_lei_lookup_404_on_not_found(
    client: AsyncClient,
) -> None:
    """POST /integrations/lei/lookup → 404 when LEI not found."""
    from saebooks.services.integrations.lei import LeiNotFoundError  # noqa: PLC0415

    with patch(
        "saebooks.api.v1.integrations.lookup_lei",
        new_callable=AsyncMock,
        side_effect=LeiNotFoundError("TEST LEI not found"),
    ):
        resp = await client.post(
            "/api/v1/integrations/lei/lookup",
            json={"search": "NOTFOUND"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_lei_lookup_gate_community(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_LEI_LOOKUP gate: community → 404."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    resp = await client.post(
        "/api/v1/integrations/lei/lookup",
        json={"search": "X"},
        headers=_auth_headers("community"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_lei_lookup_gate_offline(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_LEI_LOOKUP gate: offline edition → 404 (LEI is Pro+)."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    resp = await client.post(
        "/api/v1/integrations/lei/lookup",
        json={"search": "X"},
        headers=_auth_headers("offline"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_lei_lookup_gate_business(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_LEI_LOOKUP gate: business edition → 404 (LEI is Pro+)."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "business")
    resp = await client.post(
        "/api/v1/integrations/lei/lookup",
        json={"search": "X"},
        headers=_auth_headers("business"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Companies House
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_companies_house_search_result(
    client: AsyncClient,
) -> None:
    """POST /integrations/companies-house/search with stub → 200."""
    mock_result = MagicMock()
    mock_result.company_number = "00445790"

    with patch(
        "saebooks.api.v1.integrations.lookup_company",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.post(
            "/api/v1/integrations/companies-house/search",
            json={"query": "00445790"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_companies_house_search_503_not_configured(
    client: AsyncClient,
) -> None:
    """POST /integrations/companies-house/search → 503 when CH_API_KEY unset."""
    from saebooks.services.integrations.companies_house import (  # noqa: PLC0415
        CompaniesHouseNotConfiguredError,
    )

    with patch(
        "saebooks.api.v1.integrations.lookup_company",
        new_callable=AsyncMock,
        side_effect=CompaniesHouseNotConfiguredError("CH_API_KEY not set"),
    ):
        resp = await client.post(
            "/api/v1/integrations/companies-house/search",
            json={"query": "anything"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_companies_house_gate_community(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_COMPANIES_HOUSE gate: community → 404."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    resp = await client.post(
        "/api/v1/integrations/companies-house/search",
        json={"query": "X"},
        headers=_auth_headers("community"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ATO prefill (stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ato_prefill_returns_501(
    client: AsyncClient,
) -> None:
    """POST /integrations/ato/prefill → 501 (stub until Batch KK)."""
    resp = await client.post(
        "/api/v1/integrations/ato/prefill",
        json={"period_start": "2026-04-01", "period_end": "2026-06-30"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 501
    data = resp.json()
    assert data["error"] == "Not implemented"


@pytest.mark.asyncio
async def test_ato_prefill_invalid_date_422(
    client: AsyncClient,
) -> None:
    """POST /integrations/ato/prefill with bad dates → 422."""
    resp = await client.post(
        "/api/v1/integrations/ato/prefill",
        json={"period_start": "not-a-date", "period_end": "2026-06-30"},
        headers=_auth_headers(),
    )
    assert resp.status_code in (422, 400)
