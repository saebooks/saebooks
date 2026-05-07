"""Tests for ``saebooks.services.ato_sbr.onboarding``.

Covers the DB-bound wizard layer: get/create, step confirmation,
keystore save, SSID save, env switch, clear, status roll-up, and
the ping-then-stamp path with respx-mocked HTTP.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.ato_sbr import AtoSbrConfig
from saebooks.models.company import Company
from saebooks.services import crypto as crypto_svc
from saebooks.services.ato_sbr import onboarding as sbr


def _pkcs12(password: str = "pw", cn: str = "Acme Pty Ltd") -> bytes:
    key = rsa.generate_private_key(65537, 2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(42)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"alias",
        key,
        cert,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )


def _settings(
    *,
    fernet_key: str | None = None,
    evte_base: str = "https://evte.example",
    prod_base: str = "https://prod.example",
) -> Settings:
    key = fernet_key if fernet_key is not None else Fernet.generate_key().decode()
    return Settings(
        SAEBOOKS_FIELD_ENCRYPTION_KEY=key,
        ATO_SBR_EVTE_BASE=evte_base,
        ATO_SBR_PROD_BASE=prod_base,
    )


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


async def _reset() -> None:
    """Wipe any ato_sbr_configs row for the first company."""
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(AtoSbrConfig).where(AtoSbrConfig.company_id == company_id)
            )
        ).scalars().one_or_none()
        if row is not None:
            await session.delete(row)
            await session.commit()


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await _reset()
    yield
    await _reset()


# ---------------------------------------------------------------------- #
# get_or_create_config + status_for                                      #
# ---------------------------------------------------------------------- #


async def test_get_or_create_creates_one_row_and_status_is_all_pending() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await session.commit()
    assert config.company_id == company_id
    assert config.mode == "self_lodger"
    assert config.environment == "evte"

    status = sbr.status_for(config)
    assert all(not s.done for s in status.steps)
    assert status.ready_to_test is False
    assert status.ready_to_lodge is False
    assert status.current_step == "mygovid"


async def test_get_or_create_is_idempotent() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        a = await sbr.get_or_create_config(session, company_id)
        await session.commit()
        b = await sbr.get_or_create_config(session, company_id)
        await session.commit()
    assert a.id == b.id


# ---------------------------------------------------------------------- #
# Off-system step confirmations                                          #
# ---------------------------------------------------------------------- #


async def test_confirm_step_stamps_timestamp() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.confirm_step(session, config, "mygovid")
        await session.commit()
    assert config.mygovid_confirmed_at is not None


async def test_confirm_step_unknown_raises() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        with pytest.raises(sbr.OnboardingError):
            await sbr.confirm_step(session, config, "teleport")
        await session.rollback()


# ---------------------------------------------------------------------- #
# Keystore save                                                          #
# ---------------------------------------------------------------------- #


async def test_save_keystore_persists_ciphertext_and_metadata() -> None:
    company_id = await _first_company_id()
    s = _settings()
    data = _pkcs12(cn="Machine Cred One")
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        loaded = await sbr.save_keystore(
            session,
            config,
            data=data,
            password="pw",
            filename="keystore.xml",
            settings=s,
        )
        await session.commit()
    assert loaded.subject_cn == "Machine Cred One"
    assert config.keystore_encrypted is not None
    assert config.keystore_encrypted != data.decode("latin-1")
    assert config.keystore_password_encrypted is not None
    assert config.keystore_subject_cn == "Machine Cred One"
    assert config.keystore_not_after > datetime.now(UTC)
    # Round-trip: recover the raw bytes + password.
    recovered = crypto_svc.decrypt_field(
        config.keystore_encrypted, settings=s
    ).encode("latin-1")
    assert recovered == data
    assert (
        crypto_svc.decrypt_field(
            config.keystore_password_encrypted, settings=s
        )
        == "pw"
    )
    # Uploading the cert auto-ticks the off-system steps (the admin
    # couldn't have generated a keystore without them).
    assert config.mygovid_confirmed_at is not None
    assert config.ram_authority_confirmed_at is not None
    assert config.downloader_confirmed_at is not None


async def test_save_keystore_refuses_when_encryption_missing() -> None:
    company_id = await _first_company_id()
    s = _settings(fernet_key="")
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        with pytest.raises(sbr.OnboardingError):
            await sbr.save_keystore(
                session,
                config,
                data=_pkcs12(),
                password="pw",
                filename="k.xml",
                settings=s,
            )
        await session.rollback()


async def test_save_keystore_bubbles_keystore_error_on_bad_bytes() -> None:
    company_id = await _first_company_id()
    s = _settings()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        with pytest.raises(sbr.KeystoreError):
            await sbr.save_keystore(
                session,
                config,
                data=b"nope",
                password="pw",
                filename="k.xml",
                settings=s,
            )
        await session.rollback()


# ---------------------------------------------------------------------- #
# SSID + environment                                                     #
# ---------------------------------------------------------------------- #


async def test_save_ssid_strips_whitespace() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.save_ssid(session, config, "  12345678  ")
        await session.commit()
    assert config.ssid == "12345678"


async def test_save_ssid_rejects_empty() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        with pytest.raises(sbr.OnboardingError):
            await sbr.save_ssid(session, config, "   ")


async def test_set_environment_validates() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.set_environment(session, config, "production")
        await session.commit()
    assert config.environment == "production"

    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        with pytest.raises(sbr.OnboardingError):
            await sbr.set_environment(session, config, "staging")


# ---------------------------------------------------------------------- #
# test_environment (real HTTP mocked via respx)                          #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_test_environment_stamps_evte_on_success() -> None:
    company_id = await _first_company_id()
    s = _settings()
    respx.get("https://evte.example").mock(
        return_value=httpx.Response(200, text="ok")
    )
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        result = await sbr.test_environment(
            session, config, environment="evte", settings=s
        )
        await session.commit()
    assert result.ok is True
    assert config.evte_verified_at is not None
    assert config.prod_verified_at is None


@respx.mock
async def test_test_environment_does_not_stamp_on_failure() -> None:
    company_id = await _first_company_id()
    s = _settings()
    respx.get("https://evte.example").mock(
        return_value=httpx.Response(503)
    )
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        result = await sbr.test_environment(
            session, config, environment="evte", settings=s
        )
        await session.commit()
    assert result.ok is False
    assert config.evte_verified_at is None


@respx.mock
async def test_test_environment_production_stamps_prod() -> None:
    company_id = await _first_company_id()
    s = _settings()
    respx.get("https://prod.example").mock(
        return_value=httpx.Response(200)
    )
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.test_environment(
            session, config, environment="production", settings=s
        )
        await session.commit()
    assert config.prod_verified_at is not None


# ---------------------------------------------------------------------- #
# status roll-ups                                                        #
# ---------------------------------------------------------------------- #


async def test_status_ready_to_test_only_after_keystore_and_ssid() -> None:
    company_id = await _first_company_id()
    s = _settings()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        assert sbr.status_for(config).ready_to_test is False
        await sbr.save_keystore(
            session,
            config,
            data=_pkcs12(),
            password="pw",
            filename="k.xml",
            settings=s,
        )
        assert sbr.status_for(config).ready_to_test is False  # ssid still missing
        await sbr.save_ssid(session, config, "12345")
        assert sbr.status_for(config).ready_to_test is True
        assert sbr.status_for(config).ready_to_lodge is False


async def test_status_ready_to_lodge_after_evte_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id = await _first_company_id()
    s = _settings()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.save_keystore(
            session,
            config,
            data=_pkcs12(),
            password="pw",
            filename="k.xml",
            settings=s,
        )
        await sbr.save_ssid(session, config, "55555")
        config.evte_verified_at = datetime.now(UTC)
        assert sbr.status_for(config).ready_to_lodge is True


# ---------------------------------------------------------------------- #
# clear                                                                  #
# ---------------------------------------------------------------------- #


async def test_clear_nulls_every_column() -> None:
    company_id = await _first_company_id()
    s = _settings()
    async with AsyncSessionLocal() as session:
        config = await sbr.get_or_create_config(session, company_id)
        await sbr.save_keystore(
            session,
            config,
            data=_pkcs12(),
            password="pw",
            filename="k.xml",
            settings=s,
        )
        await sbr.save_ssid(session, config, "99999")
        await sbr.clear_config(session, config)
        await session.commit()
    assert config.keystore_encrypted is None
    assert config.keystore_password_encrypted is None
    assert config.ssid is None
    assert config.mygovid_confirmed_at is None
    assert config.environment == "evte"
