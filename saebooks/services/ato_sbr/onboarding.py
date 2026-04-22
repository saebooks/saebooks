"""Wizard orchestration for the ATO SBR Machine Credential onboarding.

Covers the "save / load / progress" side of the wizard. The router
layer owns the HTTP surface; this module owns the DB transitions and
the derived "what step is the admin on?" logic so both the form and
the progress strip render from the same source of truth.

Batch II.5 scope: store the encrypted keystore + password + SSID,
extract cert metadata for display, record per-step confirmations
and per-environment verifications. Actual SBR lodgement (signed SOAP
envelopes, token acquisition) is Batch JJ.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.ato_sbr import AtoSbrConfig
from saebooks.services import crypto as crypto_svc
from saebooks.services.ato_sbr import ping as ping_mod
from saebooks.services.ato_sbr.keystore import (
    KeystoreError,
    LoadedKeystore,
    load_keystore,
)


class OnboardingError(Exception):
    """Raised when a wizard step can't complete for a user-surfaceable reason."""


@dataclass(frozen=True)
class WizardStep:
    name: str
    label: str
    done: bool


@dataclass(frozen=True)
class WizardStatus:
    steps: tuple[WizardStep, ...]
    ready_to_test: bool
    ready_to_lodge: bool

    @property
    def current_step(self) -> str:
        for step in self.steps:
            if not step.done:
                return step.name
        return "complete"


def _now() -> datetime:
    return datetime.now(UTC)


async def get_or_create_config(
    session: AsyncSession, company_id: uuid.UUID
) -> AtoSbrConfig:
    row = (
        await session.execute(
            select(AtoSbrConfig).where(AtoSbrConfig.company_id == company_id)
        )
    ).scalars().one_or_none()
    if row is not None:
        return row
    row = AtoSbrConfig(company_id=company_id)
    session.add(row)
    await session.flush()
    return row


def status_for(config: AtoSbrConfig) -> WizardStatus:
    steps = (
        WizardStep(
            "mygovid",
            "myGovID set up with Strong identity strength",
            config.mygovid_confirmed_at is not None,
        ),
        WizardStep(
            "ram_authority",
            "Principal Authority linked for this ABN in RAM",
            config.ram_authority_confirmed_at is not None,
        ),
        WizardStep(
            "downloader",
            "Machine Credential Downloader Chrome extension installed",
            config.downloader_confirmed_at is not None,
        ),
        WizardStep(
            "keystore",
            "Machine Credential uploaded",
            config.keystore_encrypted is not None,
        ),
        WizardStep(
            "ssid",
            "Software Service ID recorded",
            bool(config.ssid),
        ),
        WizardStep(
            "evte_verified",
            "EVTE reachability confirmed",
            config.evte_verified_at is not None,
        ),
    )
    has_cert_and_ssid = (
        config.keystore_encrypted is not None and bool(config.ssid)
    )
    return WizardStatus(
        steps=steps,
        ready_to_test=has_cert_and_ssid,
        ready_to_lodge=has_cert_and_ssid and config.evte_verified_at is not None,
    )


async def confirm_step(
    session: AsyncSession, config: AtoSbrConfig, step: str
) -> None:
    """Tick an off-system step (myGovID / RAM / downloader) as confirmed."""
    field_map = {
        "mygovid": "mygovid_confirmed_at",
        "ram_authority": "ram_authority_confirmed_at",
        "downloader": "downloader_confirmed_at",
    }
    attr = field_map.get(step)
    if attr is None:
        raise OnboardingError(f"Unknown checklist step: {step!r}")
    setattr(config, attr, _now())
    await session.flush()


async def save_keystore(
    session: AsyncSession,
    config: AtoSbrConfig,
    *,
    data: bytes,
    password: str,
    filename: str,
    settings: Settings,
) -> LoadedKeystore:
    """Parse + store an uploaded RAM keystore.

    Raises ``OnboardingError`` if encryption is not configured, or
    ``KeystoreError`` (surfaced verbatim from the parser) if the
    bytes + password don't decrypt to a usable PKCS12.
    """
    if not crypto_svc.is_configured(settings):
        raise OnboardingError(
            "SAEBOOKS_FIELD_ENCRYPTION_KEY is not configured — refusing "
            "to store the keystore without at-rest encryption."
        )
    loaded = load_keystore(data, password)
    config.keystore_encrypted = crypto_svc.encrypt_field(
        data.decode("latin-1"), settings=settings
    )
    config.keystore_password_encrypted = crypto_svc.encrypt_field(
        password, settings=settings
    )
    config.keystore_filename = filename
    config.keystore_subject_cn = loaded.subject_cn
    config.keystore_issuer_cn = loaded.issuer_cn
    config.keystore_serial = loaded.serial
    config.keystore_not_before = loaded.not_before
    config.keystore_not_after = loaded.not_after
    # Uploading the cert implies steps 1-3 were completed off-system,
    # so auto-tick any still-unticked confirmations.
    now = _now()
    if config.mygovid_confirmed_at is None:
        config.mygovid_confirmed_at = now
    if config.ram_authority_confirmed_at is None:
        config.ram_authority_confirmed_at = now
    if config.downloader_confirmed_at is None:
        config.downloader_confirmed_at = now
    await session.flush()
    return loaded


async def save_ssid(
    session: AsyncSession, config: AtoSbrConfig, ssid: str
) -> None:
    cleaned = ssid.strip()
    if not cleaned:
        raise OnboardingError("SSID cannot be empty.")
    config.ssid = cleaned
    await session.flush()


async def set_environment(
    session: AsyncSession, config: AtoSbrConfig, environment: str
) -> None:
    env_norm = environment.strip().lower()
    if env_norm not in ("evte", "production"):
        raise OnboardingError(
            f"Environment must be 'evte' or 'production', got {environment!r}"
        )
    config.environment = env_norm
    await session.flush()


async def test_environment(
    session: AsyncSession,
    config: AtoSbrConfig,
    *,
    environment: str,
    settings: Settings,
) -> ping_mod.PingResult:
    """Run a reachability ping and stamp the matching verified_at."""
    env_norm = environment.strip().lower()
    if env_norm not in ("evte", "production"):
        raise OnboardingError(
            f"Environment must be 'evte' or 'production', got {environment!r}"
        )
    result = await ping_mod.ping_environment(env_norm, settings=settings)
    if result.ok:
        stamp = _now()
        if env_norm == "evte":
            config.evte_verified_at = stamp
        else:
            config.prod_verified_at = stamp
        await session.flush()
    return result


async def clear_config(session: AsyncSession, config: AtoSbrConfig) -> None:
    """Null every column except the ``id`` / ``company_id`` identity pair."""
    config.mode = "self_lodger"
    config.environment = "evte"
    config.keystore_encrypted = None
    config.keystore_password_encrypted = None
    config.keystore_filename = None
    config.keystore_subject_cn = None
    config.keystore_issuer_cn = None
    config.keystore_serial = None
    config.keystore_not_before = None
    config.keystore_not_after = None
    config.ssid = None
    config.mygovid_confirmed_at = None
    config.ram_authority_confirmed_at = None
    config.downloader_confirmed_at = None
    config.evte_verified_at = None
    config.prod_verified_at = None
    await session.flush()


# Re-export for routers / tests that want one import surface.
__all__ = [
    "KeystoreError",
    "OnboardingError",
    "WizardStatus",
    "WizardStep",
    "clear_config",
    "confirm_step",
    "get_or_create_config",
    "save_keystore",
    "save_ssid",
    "set_environment",
    "status_for",
    "test_environment",
]
