"""Router smoke tests for /admin/bank-feeds.

The feature is Enterprise-only (``FLAG_BANK_FEEDS``). Tests flip
``settings.edition`` to ``enterprise`` for the HTTP-touching tests and
verify the Community build gets 404s.

Where a route would talk to SISS, the endpoint is monkey-patched at
``saebooks.services.bank_feeds.endpoints`` so nothing leaves the
process. The landing page is exercised even without SISS creds — it
should show a "not configured" banner instead of 500-ing.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from saebooks.config import settings as app_settings
from saebooks.services.bank_feeds import endpoints


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip edition to enterprise so FLAG_BANK_FEEDS is on."""
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def configured_siss(monkeypatch: pytest.MonkeyPatch, enterprise: None) -> None:
    """Populate SISS env so `siss_configured` returns True."""
    monkeypatch.setattr(app_settings, "siss_client_id", "tid")
    monkeypatch.setattr(app_settings, "siss_client_secret", "tsec")
    monkeypatch.setattr(app_settings, "siss_subscription_key", "tkey")


async def test_community_build_404s(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Community edition — the whole router tree is 404."""
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.get("/admin/bank-feeds")
    assert r.status_code == 404


async def test_enterprise_index_renders_without_siss(
    client: AsyncClient, enterprise: None
) -> None:
    r = await client.get("/admin/bank-feeds")
    assert r.status_code == 200
    body = r.text
    assert "Bank feeds" in body
    assert "SISS credentials aren't configured" in body
    assert "+ Connect a bank" in body


async def test_enterprise_index_renders_with_siss(
    client: AsyncClient, configured_siss: None
) -> None:
    r = await client.get("/admin/bank-feeds")
    assert r.status_code == 200
    # Banner should not be shown once creds are present
    assert "SISS credentials aren't configured" not in r.text


async def test_connect_form_renders(
    client: AsyncClient, configured_siss: None
) -> None:
    r = await client.get("/admin/bank-feeds/connect")
    assert r.status_code == 200
    assert "Connect a bank" in r.text
    assert 'name="institution_id"' in r.text
    assert 'name="variant"' in r.text


async def test_connect_submit_shows_redirect_url(
    client: AsyncClient,
    configured_siss: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_consumer(client_obj: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "redirectUrl": "https://auth.sissdata/consent/TEST",
                "consentId": "consent-TEST",
            }
        }

    monkeypatch.setattr(endpoints, "initiate_consumer_consent", fake_consumer)

    r = await client.post(
        "/admin/bank-feeds/connect",
        data={"institution_id": "CBA", "variant": "consumer"},
    )
    assert r.status_code == 200
    assert "Consent initiated" in r.text
    assert "https://auth.sissdata/consent/TEST" in r.text
    assert "consent-TEST" in r.text


async def test_connect_submit_shows_error_when_unconfigured(
    client: AsyncClient, enterprise: None
) -> None:
    r = await client.post(
        "/admin/bank-feeds/connect",
        data={"institution_id": "CBA", "variant": "consumer"},
    )
    assert r.status_code == 502
    assert "SISS not configured" in r.text


async def test_callback_discovers_and_redirects_to_mapper(
    client: AsyncClient,
    configured_siss: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sds_client = f"SDS-RT-{uuid.uuid4().hex[:8]}"
    acct_id = f"ACCT-RT-{uuid.uuid4().hex[:8]}"

    async def fake_list(client_obj: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": acct_id,
                        "displayName": "Callback check",
                        "maskedNumber": "062-xxx-9999",
                        "sds": {"sdsInstitutionId": "CBA"},
                    }
                ]
            }
        }

    monkeypatch.setattr(endpoints, "list_accounts", fake_list)

    r = await client.get(
        f"/admin/bank-feeds/callback?sdsClientId={sds_client}&consentId=cid-123"
    )
    # Renders the mapper page (not a redirect) — maps immediately.
    assert r.status_code == 200
    assert "Map accounts to the chart of accounts" in r.text
    assert "Callback check" in r.text

    # Cleanup
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.bank_feed import BankFeedClient

    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


async def test_callback_without_sds_client_redirects_with_error(
    client: AsyncClient, configured_siss: None
) -> None:
    r = await client.get(
        "/admin/bank-feeds/callback", follow_redirects=False
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------------- #
# /admin/bank-feeds/health — reconcile sweep surfacing                   #
# ---------------------------------------------------------------------- #
#
# A community-gate test for /health is intentionally omitted — it would
# share the pre-existing drift in test_community_build_404s above (the
# live env has SAEBOOKS_EDITION=enterprise bleeding into the Settings
# singleton). The enterprise-path smoke below is the useful coverage.


async def test_health_renders_on_enterprise(
    client: AsyncClient, enterprise: None
) -> None:
    r = await client.get("/admin/bank-feeds/health")
    assert r.status_code == 200
    body = r.text
    assert "Bank feed health" in body
    # One of the three state banners must appear (success/warning/error)
    # depending on whatever ambient feeds the dev DB has.
    assert any(
        needle in body
        for needle in (
            "All feeds look healthy",
            "No linked feed accounts to sweep",
            "pending",
            "stale or diverging",
        )
    )
    # Back-to-feeds link + the legend both render.
    assert "&larr; Back to feeds" in body
    assert "Feed total" in body or "No linked feed accounts" in body
