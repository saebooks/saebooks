"""Company creation + seat/edition-aware cap enforcement.

``ensure_seed_company`` is the idempotent boot-time seed (no cap
check — the seed is mandatory). ``create_company`` is the cap-aware
path every human-initiated company-creation flow must go through:
CLI command, admin UI, import routines.

Company caps are always hard (CHARTER §12.3). Offline's "soft cap"
only covers admin seats, not companies.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.services import change_log as change_log_svc
from saebooks.services.licence import check_company, resolve_licence


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value."""

    def __init__(self, current: Company) -> None:
        super().__init__(
            f"Company {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


_COMPANY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "legal_name",
    "trading_name",
    "abn",
    "acn",
    "base_currency",
    "fin_year_start_month",
    "audit_mode",
    "gst_registered",
    "gst_effective_date",
    "psi_status",
    "writeoff_mode",
    "writeoff_threshold_days",
    "recovery_mode",
    "bad_debt_recovery_account",
    "costing_method",
    "phone",
    "email",
    "website",
    "default_payment_terms",
    "bank_name",
    "bank_bsb",
    "bank_account_number",
    "bank_account_name",
    "payment_terms_text",
    "terms_url",
    "version",
    "created_at",
    "archived_at",
)


def _serialise(company: Company) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload (excludes encrypted SISS fields)."""
    from datetime import date as _date
    from datetime import datetime as _dt

    data: dict[str, Any] = {}
    for key in _COMPANY_COLUMNS:
        val = getattr(company, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (_dt, _date)):
            val = val.isoformat()
        data[key] = val
    return data


class CompanyCapExceeded(Exception):
    """Raised by ``create_company`` when the active edition is at its cap.

    The message is customer-facing — router handlers can surface it
    verbatim on a flash message or upgrade CTA.
    """

    def __init__(self, *, edition: str, limit: int, current: int) -> None:
        self.edition = edition
        self.limit = limit
        self.current = current
        super().__init__(
            f"Company cap reached for the {edition} edition "
            f"({current} of {limit} companies). "
            "Upgrade to add another."
        )


async def count_active_companies(session: AsyncSession) -> int:
    """Non-archived companies count against the edition cap."""
    stmt = select(func.count(Company.id)).where(Company.archived_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


async def ensure_seed_company(session: AsyncSession) -> Company:
    """Idempotent: create the default company from env if none exists."""
    name = settings.seed_company_name or "Default Company"
    result = await session.execute(
        select(Company).where(Company.name == name, Company.archived_at.is_(None))
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    company = Company(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name=name,
        legal_name=settings.seed_company_legal_name or None,
        trading_name=settings.seed_company_trading_name or None,
        abn=settings.seed_company_abn or None,
        acn=settings.seed_company_acn or None,
        base_currency=settings.seed_company_base_currency,
        fin_year_start_month=settings.seed_company_fin_year_start_month,
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return company


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_company(
    session: AsyncSession,
    *,
    name: str,
    legal_name: str | None = None,
    trading_name: str | None = None,
    abn: str | None = None,
    acn: str | None = None,
    base_currency: str = "AUD",
    fin_year_start_month: int = 7,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> Company:
    """Create a new company, enforcing the edition cap.

    Raises ``CompanyCapExceeded`` when the active edition is already
    at its company cap.
    """
    licence = resolve_licence()
    current = await count_active_companies(session)
    check = check_company(licence.edition, current)
    if check.blocked:
        assert check.limit is not None  # blocked only happens when bounded
        raise CompanyCapExceeded(
            edition=licence.edition,
            limit=check.limit,
            current=current,
        )

    company = Company(
        tenant_id=tenant_id,
        name=name,
        legal_name=legal_name,
        trading_name=trading_name,
        abn=abn,
        acn=acn,
        base_currency=base_currency,
        fin_year_start_month=fin_year_start_month,
        version=1,
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return company


async def list_active(session: AsyncSession) -> list[Company]:
    """List all non-archived companies."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.name)
    )
    return list(result.scalars().all())


async def get(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    return await session.get(Company, company_id)


async def update(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str | None = None,
    legal_name: str | None = None,
    trading_name: str | None = None,
    abn: str | None = None,
    acn: str | None = None,
    base_currency: str | None = None,
    fin_year_start_month: int | None = None,
    audit_mode: str | None = None,
    gst_registered: bool | None = None,
    gst_effective_date: date | None = None,
    psi_status: str | None = None,
    writeoff_mode: str | None = None,
    writeoff_threshold_days: int | None = None,
    recovery_mode: str | None = None,
    bad_debt_recovery_account: str | None = None,
    costing_method: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    website: str | None = None,
    default_payment_terms: str | None = None,
    bank_name: str | None = None,
    bank_bsb: str | None = None,
    bank_account_number: str | None = None,
    bank_account_name: str | None = None,
    payment_terms_text: str | None = None,
    terms_url: str | None = None,
    address: dict | None = None,
    expected_version: int | None = None,
    actor: str = "web",
) -> Company:
    """Update company metadata. Raises VersionConflict on stale If-Match."""
    company = await session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")

    if expected_version is not None and company.version != expected_version:
        raise VersionConflict(company)

    if name is not None:
        company.name = name.strip()
    if legal_name is not None:
        company.legal_name = legal_name.strip() or None
    if trading_name is not None:
        company.trading_name = trading_name.strip() or None
    if abn is not None:
        company.abn = abn.strip() or None
    if acn is not None:
        company.acn = acn.strip() or None
    if base_currency is not None:
        company.base_currency = base_currency.strip().upper()
    if fin_year_start_month is not None:
        if not 1 <= fin_year_start_month <= 12:
            raise ValueError("fin_year_start_month must be 1–12")
        company.fin_year_start_month = fin_year_start_month
    if audit_mode is not None:
        # CHARTER §7.2 vocabulary (Wave C, migration 0185 relabelled the
        # legacy {immutable, mutable, draft} column values onto this set:
        # mutable->open, draft->hybrid, see that migration's docstring for
        # the mapping rationale). Non-immutable values are the
        # extended_audit_modes paid feature (Offline+, CHARTER §12.1) — the
        # tier gate lives in the API route (api/v1/companies.py, via
        # require_feature_inline), not here, so this service function stays
        # usable from tests/CLI/seed scripts without a Request. Enforcement
        # of what mode actually governs a posted-entry EDIT is a separate,
        # fail-safe check in services/journal.py's effective_audit_mode() —
        # writing a non-immutable value onto a below-tier company is
        # harmless because that check re-derives entitlement itself and
        # ignores the stored value when the caller's tier lacks
        # FLAG_EXTENDED_AUDIT_MODES.
        valid_modes = {"immutable", "open", "hybrid"}
        if audit_mode not in valid_modes:
            raise ValueError(f"audit_mode must be one of: {', '.join(sorted(valid_modes))}")
        company.audit_mode = audit_mode
    if gst_registered is not None:
        company.gst_registered = gst_registered
    if gst_effective_date is not None:
        company.gst_effective_date = gst_effective_date
    if psi_status is not None:
        valid = {"yes", "no", "unsure"}
        if psi_status not in valid:
            raise ValueError(f"psi_status must be one of: {sorted(valid)}")
        company.psi_status = psi_status
    if writeoff_mode is not None:
        valid_wm = {"review", "auto", "manual"}
        if writeoff_mode not in valid_wm:
            raise ValueError(f"writeoff_mode must be one of: {sorted(valid_wm)}")
        company.writeoff_mode = writeoff_mode
    if writeoff_threshold_days is not None:
        if writeoff_threshold_days <= 0:
            raise ValueError("writeoff_threshold_days must be a positive integer")
        company.writeoff_threshold_days = writeoff_threshold_days
    if recovery_mode is not None:
        valid_rm = {"smart_prompt", "manual", "reopen"}
        if recovery_mode not in valid_rm:
            raise ValueError(f"recovery_mode must be one of: {sorted(valid_rm)}")
        company.recovery_mode = recovery_mode
    if bad_debt_recovery_account is not None:
        company.bad_debt_recovery_account = (
            bad_debt_recovery_account.strip() or None
        )
    if costing_method is not None:
        valid_cm = {"weighted_average", "fifo", "quantity_only"}
        if costing_method not in valid_cm:
            raise ValueError(
                f"costing_method must be one of: {sorted(valid_cm)}"
            )
        company.costing_method = costing_method
    # Letterhead contact details + default payment terms (0171); remittance
    # fields (0168). PATCHing an empty string clears the column to NULL.
    if phone is not None:
        company.phone = phone.strip() or None
    if email is not None:
        company.email = email.strip() or None
    if website is not None:
        company.website = website.strip() or None
    if default_payment_terms is not None:
        company.default_payment_terms = default_payment_terms.strip() or None
    if bank_name is not None:
        company.bank_name = bank_name.strip() or None
    if bank_bsb is not None:
        company.bank_bsb = bank_bsb.strip() or None
    if bank_account_number is not None:
        company.bank_account_number = bank_account_number.strip() or None
    if bank_account_name is not None:
        company.bank_account_name = bank_account_name.strip() or None
    if payment_terms_text is not None:
        company.payment_terms_text = payment_terms_text.strip() or None
    if terms_url is not None:
        company.terms_url = terms_url.strip() or None
    if address is not None:
        company.address = address

    company.version = company.version + 1

    await session.flush()
    await session.refresh(company)
    await change_log_svc.append(
        session,
        entity="company",
        entity_id=company.id,
        op="update",
        actor=actor,
        payload=_serialise(company),
        version=company.version,
    )
    await session.commit()
    return company
