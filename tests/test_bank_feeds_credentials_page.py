"""Router smoke tests for /admin/bank-feeds/credentials (Batch II).

The credentials UI is gated by *two* flags:

* ``FLAG_BANK_FEEDS`` — inherited from the parent router. Community builds
  never see the page.
* ``FLAG_PER_COMPANY_SISS`` — per-route guard. Even with bank-feeds on,
  a build that hasn't unlocked the MyData-as-Vendor feature gets 404.

Plus the Fernet key must be configured before secrets can be saved.

The smoke-test endpoint (``POST /admin/bank-feeds/credentials/test``)
monkey-patches ``TokenCache.get`` to avoid a real HTTP round-trip — we
only care that the route wires resolver → token-cache end-to-end and
redirects with the right status badge.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import crypto as crypto_svc
from saebooks.services.bank_feeds import token as token_mod


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    k = Fernet.generate_key().decode()
    monkeypatch.setattr(app_settings, "field_encryption_key", k)
    return k


@pytest.fixture
def configured_env_siss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate env-var SISS so the fallback path has something."""
    monkeypatch.setattr(app_settings, "siss_client_id", "env-client")
    monkeypatch.setattr(app_settings, "siss_client_secret", "env-secret")
    monkeypatch.setattr(app_settings, "siss_subscription_key", "env-key")


async def _first_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert row is not None
        return row.id


async def _clear_company_creds() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, company_id)
        assert row is not None
        row.siss_client_id = None
        row.siss_client_secret_encrypted = None
        row.siss_subscription_key_encrypted = None
        row.siss_environment = None
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_creds() -> Any:
    """Each test starts + ends with the company's SISS creds cleared."""
    await _clear_company_creds()
    yield
    await _clear_company_creds()


# ---------------------------------------------------------------------- #
# Feature gating                                                         #
# ---------------------------------------------------------------------- #


async def test_community_build_404s(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")
    r = await client.get("/admin/bank-feeds/credentials")
    assert r.status_code == 404


async def test_enterprise_form_renders(
    client: AsyncClient, enterprise: None, fernet_key: str
) -> None:
    r = await client.get("/admin/bank-feeds/credentials")
    assert r.status_code == 200
    assert "SISS credentials" in r.text
    assert "Update credentials" in r.text
    assert "Smoke-test" in r.text


async def test_form_warns_when_encryption_key_missing(
    client: AsyncClient, enterprise: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "field_encryption_key", "")
    r = await client.get("/admin/bank-feeds/credentials")
    assert r.status_code == 200
    assert "SAEBOOKS_FIELD_ENCRYPTION_KEY" in r.text


# ---------------------------------------------------------------------- #
# Save / clear                                                           #
# ---------------------------------------------------------------------- #


async def test_save_persists_encrypted_secret(
    client: AsyncClient, enterprise: None, fernet_key: str
) -> None:
    r = await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "my-co-client",
            "client_secret": "my-co-secret",
            "subscription_key": "my-co-key",
            "environment": "sandbox",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "credentials+saved" in r.headers["location"]

    async with AsyncSessionLocal() as session:
        row = await session.get(Company, await _first_company_id())
        assert row is not None
        assert row.siss_client_id == "my-co-client"
        assert row.siss_environment == "sandbox"
        # Ciphertext, not plaintext.
        assert row.siss_client_secret_encrypted is not None
        assert row.siss_client_secret_encrypted != "my-co-secret"
        assert row.siss_subscription_key_encrypted is not None
        # Round-trip recovers the plaintext.
        assert (
            crypto_svc.decrypt_field(row.siss_client_secret_encrypted)
            == "my-co-secret"
        )
        assert (
            crypto_svc.decrypt_field(row.siss_subscription_key_encrypted)
            == "my-co-key"
        )


async def test_save_without_encryption_key_bounces_with_error(
    client: AsyncClient, enterprise: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "field_encryption_key", "")
    r = await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "x",
            "client_secret": "y",
            "subscription_key": "z",
            "environment": "production",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=encryption" in r.headers["location"]
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, await _first_company_id())
        assert row is not None
        assert row.siss_client_id is None
        assert row.siss_client_secret_encrypted is None


async def test_save_rejects_unknown_environment(
    client: AsyncClient, enterprise: None, fernet_key: str
) -> None:
    r = await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "x",
            "client_secret": "y",
            "subscription_key": "z",
            "environment": "staging",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid+environment" in r.headers["location"]


async def test_save_blank_secrets_preserves_existing(
    client: AsyncClient, enterprise: None, fernet_key: str
) -> None:
    # Seed a first save.
    await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "first",
            "client_secret": "first-secret",
            "subscription_key": "first-key",
            "environment": "production",
        },
        follow_redirects=False,
    )
    # Second save with blank secret fields must not wipe them.
    r = await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "second",
            "client_secret": "",
            "subscription_key": "",
            "environment": "production",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, await _first_company_id())
        assert row is not None
        assert row.siss_client_id == "second"
        assert row.siss_client_secret_encrypted is not None
        assert (
            crypto_svc.decrypt_field(row.siss_client_secret_encrypted)
            == "first-secret"
        )


async def test_clear_drops_all_creds(
    client: AsyncClient, enterprise: None, fernet_key: str
) -> None:
    await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "drop-me",
            "client_secret": "s",
            "subscription_key": "k",
            "environment": "production",
        },
        follow_redirects=False,
    )
    r = await client.post(
        "/admin/bank-feeds/credentials/clear", follow_redirects=False
    )
    assert r.status_code == 303
    assert "credentials+cleared" in r.headers["location"]
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, await _first_company_id())
        assert row is not None
        assert row.siss_client_id is None
        assert row.siss_client_secret_encrypted is None


# ---------------------------------------------------------------------- #
# Smoke test                                                             #
# ---------------------------------------------------------------------- #


async def test_smoke_test_ok_uses_env_when_company_empty(
    client: AsyncClient,
    enterprise: None,
    fernet_key: str,
    configured_env_siss: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: Any) -> str:
        return "fake-bearer-token"

    monkeypatch.setattr(token_mod.TokenCache, "get", fake_get)
    r = await client.post(
        "/admin/bank-feeds/credentials/test", follow_redirects=False
    )
    assert r.status_code == 303
    assert "test=ok" in r.headers["location"]
    assert "source%3Aenv" in r.headers["location"]


async def test_smoke_test_ok_uses_company_when_populated(
    client: AsyncClient,
    enterprise: None,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: Any) -> str:
        return "fake-bearer"

    monkeypatch.setattr(token_mod.TokenCache, "get", fake_get)
    # Seed per-company creds.
    await client.post(
        "/admin/bank-feeds/credentials",
        data={
            "client_id": "co-cli",
            "client_secret": "co-sec",
            "subscription_key": "co-key",
            "environment": "production",
        },
        follow_redirects=False,
    )
    r = await client.post(
        "/admin/bank-feeds/credentials/test", follow_redirects=False
    )
    assert r.status_code == 303
    assert "test=ok" in r.headers["location"]
    assert "source%3Acompany" in r.headers["location"]


async def test_smoke_test_failure_surfaces_error(
    client: AsyncClient,
    enterprise: None,
    fernet_key: str,
    configured_env_siss: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: Any) -> str:
        raise RuntimeError("upstream unreachable")

    monkeypatch.setattr(token_mod.TokenCache, "get", fake_get)
    r = await client.post(
        "/admin/bank-feeds/credentials/test", follow_redirects=False
    )
    assert r.status_code == 303
    assert "test=fail" in r.headers["location"]
    assert "upstream%20unreachable" in r.headers["location"]


async def test_smoke_test_when_nothing_configured_bounces(
    client: AsyncClient,
    enterprise: None,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear env SISS too.
    monkeypatch.setattr(app_settings, "siss_client_id", "")
    monkeypatch.setattr(app_settings, "siss_client_secret", "")
    monkeypatch.setattr(app_settings, "siss_subscription_key", "")
    r = await client.post(
        "/admin/bank-feeds/credentials/test", follow_redirects=False
    )
    assert r.status_code == 303
    assert "test=fail" in r.headers["location"]
