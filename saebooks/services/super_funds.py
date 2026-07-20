"""Super-fund service — CRUD + company-default flag enforcement.

APRA funds carry a USI (Unique Superannuation Identifier, 11 chars).
SMSFs carry ABN + ESA + bank account details (encrypted). Those
vehicle-specific rules are AU law and live in the AU jurisdiction
module (``jurisdictions.au.super_funds``, Phase 1); this module keeps
the jurisdiction-neutral mechanics (CRUD, default-flag invariant,
encrypted bank fields).

Neutral-core strip (Job D): this module used to reach into
``jurisdictions.au.super_funds`` directly with a lazy import inside
``create()`` — a core→module edge, and it applied AU's SMSF/USI rules
to every company regardless of jurisdiction (super funds were "AU-only
vehicles today", per the removed docstring note that used to live on
``create()``). It now dispatches vehicle-law validation through a tiny
per-jurisdiction registry (``register_retirement_validator``/
``_validate_retirement_account`` below), the same registration-inversion
shape as ``services.payroll``'s engine/posting-profile registries: each
jurisdiction module self-registers its validator via
``services.jurisdiction_modules.register_jurisdiction_module(...,
retirement_validator=...)`` at import time (see
``jurisdictions/au/__init__.py``); an unregistered jurisdiction
(including the neutral ``"XX"`` sentinel, or any non-AU company today)
degrades to a no-op — no vehicle-law rule enforced, same null-object
contract as ``payroll.get_payroll_engine``. AU behaviour is
byte-identical: every existing company defaults to
``jurisdiction="AU"`` and resolves to the exact same
``validate_fund_fields`` call as before, just reached generically.

The "exactly one default per company" invariant is enforced via a
partial unique index in the migration:

    UNIQUE (company_id) WHERE is_default = TRUE AND archived_at IS NULL

The service layer flips ``is_default`` atomically: when setting a
new default, the previous default is cleared in the same txn so the
partial index never sees two defaults.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.company import Company
from saebooks.models.super_fund import SuperFund
from saebooks.services import crypto

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class SuperFundError(Exception):
    def __init__(self, message: str, *, code: str = "super_fund_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Retirement-vehicle-law validation registry (neutral-core strip Job D)
# ---------------------------------------------------------------------------
# Keyed on ``Company.jurisdiction``, exactly like ``services.payroll``'s
# ``_REGISTRY``/``_POSTING_PROFILES``. Starts empty; populated by each
# jurisdiction module's self-registration
# (``jurisdiction_modules.register_jurisdiction_module(...,
# retirement_validator=...)``). Unregistered jurisdictions degrade to
# "validate nothing" — a generic retirement account is always creatable
# even with no vehicle-law module bolted on.
_RETIREMENT_VALIDATORS: dict[str, Callable[..., None]] = {}

# One-shot guard for the lazy jurisdiction-bootstrap import below — same
# per-reader-flag idiom as ``services.payroll._modules_loaded`` (the
# app-wide ``bootstrap.jurisdictions.ensure_loaded()`` it calls is
# itself idempotent; this local flag just avoids re-entering it on
# every ``create()`` call).
_modules_loaded = False


def register_retirement_validator(
    jurisdiction: str, validator: Callable[..., None]
) -> None:
    """Register (or replace) the retirement-account vehicle-law
    validator for a jurisdiction. Called by that jurisdiction's
    self-registration, never by core code directly.
    """
    _RETIREMENT_VALIDATORS[jurisdiction] = validator


def _ensure_modules_registered() -> None:
    global _modules_loaded
    if not _modules_loaded:
        # Local import to keep this module import-light and avoid a
        # module-level cycle — same rationale as
        # ``services.payroll._ensure_modules_registered``.
        from saebooks.bootstrap.jurisdictions import ensure_loaded

        ensure_loaded()
        _modules_loaded = True


def _validate_retirement_account(
    jurisdiction: str,
    *,
    is_smsf: bool,
    usi: str | None,
    employer_abn: str | None,
    esa: str | None,
) -> None:
    """Dispatch vehicle-law validation to the company's jurisdiction
    module. Registered code → that module's validator; anything else
    (the ``"XX"`` sentinel, or a jurisdiction with no retirement-vehicle
    module) is a no-op — never raises.
    """
    _ensure_modules_registered()
    validator = _RETIREMENT_VALIDATORS.get(jurisdiction)
    if validator is not None:
        validator(is_smsf=is_smsf, usi=usi, employer_abn=employer_abn, esa=esa)


def _encrypt_opt(value: str | None) -> str | None:
    return crypto.encrypt_field(value) if value else None


def _decrypt_opt(value: str | None) -> str | None:
    return crypto.decrypt_field(value) if value else None


@dataclass
class SuperFundDecrypted:
    """Lightweight view object with SMSF bank fields decrypted.

    Use only when the caller needs plaintext (e.g. SAFF CSV export,
    pay-run ABA file generation). Audit-log every decryption.
    """

    smsf_bsb: str | None
    smsf_account_number: str | None
    smsf_account_name: str | None


async def create(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    name: str,
    is_smsf: bool = False,
    usi: str | None = None,
    employer_abn: str | None = None,
    esa: str | None = None,
    smsf_bsb: str | None = None,
    smsf_account_number: str | None = None,
    smsf_account_name: str | None = None,
    is_default: bool = False,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> SuperFund:
    # Validate combinations early so the CHECK constraint isn't the first
    # error. The USI/SMSF rules are AU vehicle law; dispatch through the
    # per-jurisdiction registry (see module docstring) rather than
    # importing the AU package directly — resolves ``Company.jurisdiction``
    # exactly like ``journal._apply_tax_treatment``/``pay_runs_v2._compute``
    # do, defaulting to "AU" when the company row can't be found (matches
    # ``Company.jurisdiction``'s own column default). Every existing
    # company is jurisdiction="AU" today, so this resolves to the exact
    # same ``validate_fund_fields`` call as before — byte-identical.
    company_jurisdiction = (
        await session.execute(
            sa.select(Company.jurisdiction).where(Company.id == company_id)
        )
    ).scalar_one_or_none() or "AU"
    _validate_retirement_account(
        company_jurisdiction,
        is_smsf=is_smsf, usi=usi, employer_abn=employer_abn, esa=esa,
    )

    fund = SuperFund(
        company_id=company_id,
        tenant_id=tenant_id,
        name=name.strip(),
        usi=usi,
        is_smsf=is_smsf,
        employer_abn=employer_abn,
        esa=esa,
        smsf_bsb_encrypted=_encrypt_opt(smsf_bsb),
        smsf_account_number_encrypted=_encrypt_opt(smsf_account_number),
        smsf_account_name_encrypted=_encrypt_opt(smsf_account_name),
        is_default=False,  # set via dedicated path below
    )
    session.add(fund)
    await session.flush()
    await session.refresh(fund)

    if is_default:
        await set_default(session, company_id=company_id, fund_id=fund.id)
        await session.refresh(fund)
    return fund


async def get(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    fund_id: uuid.UUID,
) -> SuperFund | None:
    stmt = sa.select(SuperFund).where(
        SuperFund.company_id == company_id,
        SuperFund.id == fund_id,
        SuperFund.archived_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_funds(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> tuple[list[SuperFund], int]:
    where = [SuperFund.company_id == company_id]
    if not include_archived:
        where.append(SuperFund.archived_at.is_(None))
    count_stmt = sa.select(sa.func.count()).select_from(SuperFund).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()
    items_stmt = (
        sa.select(SuperFund)
        .where(*where)
        .order_by(SuperFund.is_default.desc(), SuperFund.name)
        .limit(limit).offset(offset)
    )
    items = list((await session.execute(items_stmt)).scalars().all())
    return items, int(total)


async def get_default(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
) -> SuperFund | None:
    stmt = sa.select(SuperFund).where(
        SuperFund.company_id == company_id,
        SuperFund.is_default.is_(True),
        SuperFund.archived_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_default(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    fund_id: uuid.UUID,
) -> SuperFund:
    """Atomically move the default flag to ``fund_id``.

    Clears any existing default in the same txn so the partial-unique
    index never sees two defaults. Caller commits.
    """
    # Clear current default.
    await session.execute(
        sa.update(SuperFund)
        .where(
            SuperFund.company_id == company_id,
            SuperFund.is_default.is_(True),
            SuperFund.archived_at.is_(None),
        )
        .values(is_default=False, version=SuperFund.version + 1)
    )
    # Set new default.
    fund = await get(session, company_id=company_id, fund_id=fund_id)
    if fund is None:
        raise SuperFundError("super fund not found", code="not_found")
    fund.is_default = True
    fund.version += 1
    await session.flush()
    await session.refresh(fund)
    return fund


async def update(
    session: AsyncSession,
    *,
    fund: SuperFund,
    expected_version: int | None = None,
    **fields: Any,
) -> SuperFund:
    if expected_version is not None and fund.version != expected_version:
        raise SuperFundError(
            f"version mismatch: expected {expected_version}, got {fund.version}",
            code="version_mismatch",
        )

    SIMPLE = {"name", "usi", "is_smsf", "employer_abn", "esa"}
    ENCRYPTED_MAP = {
        "smsf_bsb": "smsf_bsb_encrypted",
        "smsf_account_number": "smsf_account_number_encrypted",
        "smsf_account_name": "smsf_account_name_encrypted",
    }

    for name, value in fields.items():
        if name in SIMPLE:
            setattr(fund, name, value.strip() if isinstance(value, str) else value)
        elif name in ENCRYPTED_MAP:
            setattr(fund, ENCRYPTED_MAP[name], _encrypt_opt(value))
        # silently ignore unknown — strict at API boundary

    fund.version += 1
    await session.flush()
    await session.refresh(fund)
    return fund


async def archive(
    session: AsyncSession, *, fund: SuperFund
) -> SuperFund:
    if fund.is_default:
        raise SuperFundError(
            "cannot archive the company default super fund; set another as default first",
            code="cannot_archive_default",
        )
    fund.archived_at = datetime.now(UTC)
    await session.flush()
    return fund


def decrypt_smsf_bank(fund: SuperFund) -> SuperFundDecrypted:
    """Return plaintext SMSF bank fields. Caller MUST audit-log access."""
    return SuperFundDecrypted(
        smsf_bsb=_decrypt_opt(fund.smsf_bsb_encrypted),
        smsf_account_number=_decrypt_opt(fund.smsf_account_number_encrypted),
        smsf_account_name=_decrypt_opt(fund.smsf_account_name_encrypted),
    )
