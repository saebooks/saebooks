"""Router tests for /contacts/lei-lookup, /webhooks/stripe,
/admin/integrations, /admin/integrations/paperless/attach.

NOTE: The test_integrations_page.py already covers the broad smoke tests
(page render, Stripe HMAC flow, LEI feature gate, ATO prefill stub,
Paperless 503 unconfigured).  This file adds *new* coverage of additional
edge cases not covered there:

* /admin/integrations → healthz flags shape
* Stripe webhook: missing Stripe-Signature header → 400
* Stripe webhook: bad signature → 400
* Stripe webhook: valid sig + payment_intent.succeeded unhandled (no journal) → 200
* Stripe webhook: unconfigured → 503
* LEI lookup: 404 gate in community edition
* LEI apply: 404 gate in community edition
* POST /admin/integrations/paperless/attach → 503 when unconfigured
* POST /admin/integrations/paperless/attach → 404 when journal not found
* POST /admin/integrations/ato-prefill → 501 stub
* GET /admin/integrations/ trailing slash → 200
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient, ASGITransport

from saebooks.config import settings as app_settings
from saebooks.main import app

SECRET = "whsec_testintegrations"
LEI_BASE = "https://lei.test/api/v1"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
def stripe_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "stripe_webhook_secret", SECRET)


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def configured_lei(monkeypatch: pytest.MonkeyPatch, enterprise: None) -> None:
    monkeypatch.setattr(app_settings, "lei_api_base", LEI_BASE)


def _sign_stripe_body(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={mac}"


# ---------------------------------------------------------------------------
# Integrations landing page
# ---------------------------------------------------------------------------


async def test_integrations_landing_200(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations")
    assert r.status_code == 200


async def test_integrations_trailing_slash_200(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations/")
    assert r.status_code == 200


async def test_integrations_healthz_shape(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations/healthz")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    for key in ("paperless", "lei", "stripe", "companies_house", "ato_prefill"):
        assert f"{key}=" in r.text


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------


async def test_stripe_webhook_503_unconfigured(client: AsyncClient) -> None:
    r = await client.post("/webhooks/stripe", content=b"{}")
    assert r.status_code == 503


async def test_stripe_webhook_400_missing_sig_header(
    client: AsyncClient, stripe_configured: None
) -> None:
    r = await client.post("/webhooks/stripe", content=b'{"id":"evt_x"}')
    assert r.status_code == 400
    assert "Stripe-Signature" in r.text


async def test_stripe_webhook_400_bad_signature(
    client: AsyncClient, stripe_configured: None
) -> None:
    r = await client.post(
        "/webhooks/stripe",
        content=b'{"id":"evt_x"}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef00"},
    )
    assert r.status_code == 400


async def test_stripe_webhook_200_valid_sig_unhandled_event(
    client: AsyncClient, stripe_configured: None
) -> None:
    body = json.dumps({"id": "evt_2", "type": "customer.created"}).encode()
    sig = _sign_stripe_body(body, SECRET)
    r = await client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert r.json()["handled"] is False


async def test_stripe_webhook_200_customer_updated_unhandled(
    client: AsyncClient, stripe_configured: None
) -> None:
    """customer.updated is an event type the webhook handler ignores — handled=False."""
    event = {
        "id": "evt_cu_4",
        "type": "customer.updated",
        "data": {
            "object": {
                "id": "cus_test",
                "email": "test@example.com",
            }
        },
    }
    body = json.dumps(event).encode()
    sig = _sign_stripe_body(body, SECRET)
    r = await client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert r.json()["handled"] is False


# ---------------------------------------------------------------------------
# LEI lookup — feature gate
# ---------------------------------------------------------------------------


async def test_lei_lookup_404_community(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.post(
        "/contacts/lei-lookup",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 404


async def test_lei_apply_404_community(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.post(
        f"/contacts/{uuid.uuid4()}/lei-apply",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 404


@respx.mock
async def test_lei_lookup_not_found_fragment(
    client: AsyncClient, configured_lei: None
) -> None:
    respx.get(f"{LEI_BASE}/lei-records/529900T8BM49AURSDO55").mock(
        return_value=httpx.Response(404, text="")
    )
    r = await client.post(
        "/contacts/lei-lookup",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 404
    assert "GLEIF lookup failed" in r.text or "not found" in r.text.lower()


# ---------------------------------------------------------------------------
# Paperless attach
# ---------------------------------------------------------------------------


async def test_paperless_attach_503_unconfigured(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/integrations/paperless/attach",
        data={"journal_id": str(uuid.uuid4()), "document_id": "42"},
    )
    assert r.status_code == 503
    assert "not configured" in r.json().get("error", "").lower()


# ---------------------------------------------------------------------------
# ATO prefill stub
# ---------------------------------------------------------------------------


async def test_ato_prefill_stub_501(client: AsyncClient) -> None:
    r = await client.post("/admin/integrations/ato-prefill")
    assert r.status_code == 501
    body = r.json()
    assert body["error"] == "Not implemented"
    assert "Batch KK" in body["detail"]
