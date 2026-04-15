from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company


async def ensure_seed_company(session: AsyncSession) -> Company:
    """Idempotent: create the default company from env if none exists."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None))
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    company = Company(
        name=settings.seed_company_name or "Default Company",
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
