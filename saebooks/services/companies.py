"""Company creation + seat/edition-aware cap enforcement.

``ensure_seed_company`` is the idempotent boot-time seed (no cap
check — the seed is mandatory). ``create_company`` is the cap-aware
path every human-initiated company-creation flow must go through:
CLI command, admin UI, import routines.

Company caps are always hard (CHARTER §12.3). Offline's "soft cap"
only covers admin seats, not companies.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.services.licence import check_company, resolve_licence


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
    )
    session.add(company)
    await session.commit()
    await session.refresh(company)
    return company
