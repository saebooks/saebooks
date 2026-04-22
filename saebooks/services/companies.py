"""Company creation + seat/edition-aware cap enforcement.

``ensure_seed_company`` is the idempotent boot-time seed (no cap
check — the seed is mandatory). ``create_company`` is the cap-aware
path every human-initiated company-creation flow must go through:
CLI command, admin UI, import routines.

Company caps are always hard (CHARTER §12.3). Offline's "soft cap"
only covers admin seats, not companies.
"""
from __future__ import annotations

from typing import Any
import uuid

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
    "version",
    "created_at",
    "archived_at",
)


def _serialise(company: Company) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload (excludes encrypted SISS fields)."""
    from datetime import datetime as _dt

    data: dict[str, Any] = {}
    for key in _COMPANY_COLUMNS:
        val = getattr(company, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, _dt):
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
        valid_modes = {"immutable", "mutable", "draft"}
        if audit_mode not in valid_modes:
            raise ValueError(f"audit_mode must be one of: {', '.join(sorted(valid_modes))}")
        company.audit_mode = audit_mode

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
