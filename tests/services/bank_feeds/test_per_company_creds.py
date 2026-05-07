"""Tests for Batch II per-company SISS credential resolution.

Covers:

* Flag-off  → resolve_company_siss_creds uses env-var creds (source="env")
* Flag-on + blank company row → still uses env-var creds
* Flag-on + populated company row → decrypts + returns those (source="company")
* Flag-on + encrypted columns present but encryption-key absent → raises
  SissNotConfiguredError rather than silently falling through to env
  (the column ciphertext is meaningful — ignoring it would be wrong).
* SissClient.from_company classmethod delegates to the resolver
* siss_client_for_company async context manager yields a live client
* Whole fallback path when neither env nor per-company creds exist → raises
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import crypto as crypto_svc
from saebooks.services.bank_feeds import onboarding
from saebooks.services.bank_feeds.client import SissClient


def _settings(
    *,
    edition: str = "enterprise",
    env_configured: bool = True,
    fernet_key: str | None = None,
    **overrides: Any,
) -> Settings:
    key = fernet_key if fernet_key is not None else Fernet.generate_key().decode()
    base: dict[str, Any] = dict(
        SAEBOOKS_EDITION=edition,
        SAEBOOKS_FIELD_ENCRYPTION_KEY=key,
        SISS_TOKEN_URL="https://auth.example/oauth/token",
        SISS_API_BASE="https://api.example/cdr-au/v1/",
    )
    if env_configured:
        base["SISS_CLIENT_ID"] = "env-client"
        base["SISS_CLIENT_SECRET"] = "env-secret"
        base["SISS_SUBSCRIPTION_KEY"] = "env-key"
    base.update(overrides)
    return Settings(**base)


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


async def _set_company_creds(
    company_id: uuid.UUID,
    *,
    client_id: str | None,
    client_secret_ct: str | None,
    subscription_key_ct: str | None,
    environment: str | None,
) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, company_id)
        assert row is not None
        row.siss_client_id = client_id
        row.siss_client_secret_encrypted = client_secret_ct
        row.siss_subscription_key_encrypted = subscription_key_ct
        row.siss_environment = environment
        await session.commit()


async def _clear_company_creds(company_id: uuid.UUID) -> None:
    await _set_company_creds(
        company_id,
        client_id=None,
        client_secret_ct=None,
        subscription_key_ct=None,
        environment=None,
    )


# ---------------------------------------------------------------------- #
# Resolver                                                               #
# ---------------------------------------------------------------------- #


async def test_resolver_falls_back_to_env_when_flag_off() -> None:
    company_id = await _first_company_id()
    s = _settings(edition="community")
    async with AsyncSessionLocal() as session:
        creds = await onboarding.resolve_company_siss_creds(
            session, company_id, settings=s
        )
    assert creds.source == "env"
    assert creds.client_id == "env-client"
    assert creds.client_secret == "env-secret"
    assert creds.subscription_key == "env-key"


async def test_resolver_falls_back_to_env_when_flag_on_but_no_creds_stored() -> None:
    company_id = await _first_company_id()
    await _clear_company_creds(company_id)
    s = _settings(edition="enterprise")
    async with AsyncSessionLocal() as session:
        creds = await onboarding.resolve_company_siss_creds(
            session, company_id, settings=s
        )
    assert creds.source == "env"
    assert creds.client_id == "env-client"


async def test_resolver_picks_per_company_creds_when_flag_on() -> None:
    company_id = await _first_company_id()
    s = _settings(edition="enterprise")
    secret_ct = crypto_svc.encrypt_field("per-co-secret", settings=s)
    subkey_ct = crypto_svc.encrypt_field("per-co-key", settings=s)
    await _set_company_creds(
        company_id,
        client_id="per-co-client",
        client_secret_ct=secret_ct,
        subscription_key_ct=subkey_ct,
        environment="sandbox",
    )
    try:
        async with AsyncSessionLocal() as session:
            creds = await onboarding.resolve_company_siss_creds(
                session, company_id, settings=s
            )
        assert creds.source == "company"
        assert creds.client_id == "per-co-client"
        assert creds.client_secret == "per-co-secret"
        assert creds.subscription_key == "per-co-key"
        assert creds.environment == "sandbox"
    finally:
        await _clear_company_creds(company_id)


async def test_resolver_raises_when_flag_on_and_encryption_key_missing() -> None:
    """Encrypted columns present but no key → surface a clear error.

    Silently falling through to env would hide a misconfigured install.
    """
    company_id = await _first_company_id()
    # Populate creds using a real key, then probe with a key-less Settings.
    populated = _settings(edition="enterprise")
    secret_ct = crypto_svc.encrypt_field("x", settings=populated)
    subkey_ct = crypto_svc.encrypt_field("y", settings=populated)
    await _set_company_creds(
        company_id,
        client_id="per-co-client",
        client_secret_ct=secret_ct,
        subscription_key_ct=subkey_ct,
        environment="production",
    )
    try:
        no_key = _settings(edition="enterprise", fernet_key="")
        async with AsyncSessionLocal() as session:
            with pytest.raises(onboarding.SissNotConfiguredError):
                await onboarding.resolve_company_siss_creds(
                    session, company_id, settings=no_key
                )
    finally:
        await _clear_company_creds(company_id)


async def test_resolver_raises_when_env_and_company_both_blank() -> None:
    company_id = await _first_company_id()
    await _clear_company_creds(company_id)
    s = _settings(edition="enterprise", env_configured=False)
    async with AsyncSessionLocal() as session:
        with pytest.raises(onboarding.SissNotConfiguredError):
            await onboarding.resolve_company_siss_creds(
                session, company_id, settings=s
            )


# ---------------------------------------------------------------------- #
# SissClient.from_company + siss_client_for_company                      #
# ---------------------------------------------------------------------- #


async def test_from_company_classmethod_returns_client() -> None:
    company_id = await _first_company_id()
    await _clear_company_creds(company_id)
    s = _settings(edition="enterprise")
    async with AsyncSessionLocal() as session:
        client = await SissClient.from_company(session, company_id, settings=s)
        assert isinstance(client, SissClient)
        async with client:
            # Context manager must enter/exit cleanly even though we don't
            # issue any requests.
            pass


async def test_siss_client_for_company_ctx_manager_yields_client() -> None:
    company_id = await _first_company_id()
    await _clear_company_creds(company_id)
    s = _settings(edition="enterprise")
    async with (
        AsyncSessionLocal() as session,
        onboarding.siss_client_for_company(
            session, company_id, settings=s
        ) as client,
    ):
        assert isinstance(client, SissClient)
