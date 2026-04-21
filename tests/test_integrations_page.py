"""Router smoke tests for /admin/integrations, /webhooks/stripe,
/contacts/lei-* .

Covers:

* /admin/integrations landing + /healthz
* Stripe webhook sig handling (unconfigured, missing header, bad sig, ok)
* LEI lookup feature-gate (404 in Community)
* ATO prefill stub returns 501
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient

from saebooks.config import settings as app_settings

SECRET = "whsec_testsecret"
LEI_BASE = "https://lei.example/api/v1"


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def configured_lei(monkeypatch: pytest.MonkeyPatch, enterprise: None) -> None:
    monkeypatch.setattr(app_settings, "lei_api_base", LEI_BASE)


@pytest.fixture
def stripe_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "stripe_webhook_secret", SECRET)


async def test_integrations_index_renders(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations")
    assert r.status_code == 200
    body = r.text
    assert "Paperless" in body
    assert "LEI" in body or "GLEIF" in body
    assert "Stripe" in body
    assert "ATO" in body


async def test_integrations_index_trailing_slash(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations/")
    assert r.status_code == 200


async def test_integrations_healthz_is_plain_text(client: AsyncClient) -> None:
    r = await client.get("/admin/integrations/healthz")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # community default — lei off, stripe off, paperless off, ato off
    assert "lei=" in r.text
    assert "stripe=" in r.text
    assert "paperless=" in r.text
    assert "ato_prefill=" in r.text


# ----- Stripe webhook ----- #


async def test_stripe_webhook_503_when_unconfigured(client: AsyncClient) -> None:
    r = await client.post("/webhooks/stripe", content=b"{}")
    assert r.status_code == 503


async def test_stripe_webhook_400_when_header_missing(
    client: AsyncClient, stripe_configured: None
) -> None:
    r = await client.post("/webhooks/stripe", content=b"{}")
    assert r.status_code == 400
    assert "Stripe-Signature" in r.text


async def test_stripe_webhook_400_on_bad_signature(
    client: AsyncClient, stripe_configured: None
) -> None:
    r = await client.post(
        "/webhooks/stripe",
        content=b'{"id":"evt_1"}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400


async def test_stripe_webhook_2xx_on_valid_sig_unknown_event(
    client: AsyncClient, stripe_configured: None
) -> None:
    """Valid sig + event type we don't handle — 200, handled=False."""
    import time

    body = json.dumps({"id": "evt_1", "type": "invoice.paid"}).encode()
    ts = int(time.time())
    mac = hmac.new(
        SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    r = await client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": f"t={ts},v1={mac}"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["received"] is True
    assert payload["handled"] is False


# ----- LEI lookup (feature-gated) ----- #


async def test_lei_lookup_404_in_community(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Host may be running SAEBOOKS_EDITION=enterprise (books.sauer.com.au).
    # Force community here so the gate test is deterministic regardless of
    # container env.
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.post(
        "/contacts/lei-lookup",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 404


async def test_lei_apply_404_in_community(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.post(
        f"/contacts/{uuid.uuid4()}/lei-apply",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 404


@respx.mock
async def test_lei_lookup_returns_fragment_in_enterprise(
    client: AsyncClient, configured_lei: None
) -> None:
    payload = {
        "data": {
            "type": "lei-records",
            "id": "529900T8BM49AURSDO55",
            "attributes": {
                "lei": "529900T8BM49AURSDO55",
                "entity": {
                    "legalName": {"name": "GlobalBank AG"},
                    "jurisdiction": "DE",
                    "status": "ACTIVE",
                },
                "registration": {"status": "ISSUED"},
            },
        }
    }
    respx.get(f"{LEI_BASE}/lei-records/529900T8BM49AURSDO55").mock(
        return_value=httpx.Response(200, json=payload)
    )
    r = await client.post(
        "/contacts/lei-lookup",
        data={"lei": "529900T8BM49AURSDO55"},
    )
    assert r.status_code == 200
    assert "GLEIF match" in r.text
    assert "GlobalBank AG" in r.text


@respx.mock
async def test_lei_lookup_404_fragment_on_not_found(
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
    assert "GLEIF lookup failed" in r.text


# ----- ATO prefill stub ----- #


async def test_ato_prefill_stub_returns_501(client: AsyncClient) -> None:
    r = await client.post("/admin/integrations/ato-prefill")
    assert r.status_code == 501
    body = r.json()
    assert "Not implemented" in body["error"]
    assert "Batch KK" in body["detail"]


# ----- Paperless attach (unconfigured path) ----- #


async def test_paperless_attach_503_when_unconfigured(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/integrations/paperless/attach",
        data={"journal_id": str(uuid.uuid4()), "document_id": "1"},
    )
    assert r.status_code == 503
    assert "not configured" in r.text
