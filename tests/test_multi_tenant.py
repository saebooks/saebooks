from sqlalchemy import func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company


async def test_schema_permits_multiple_companies() -> None:
    async with AsyncSessionLocal() as session:
        session.add(Company(name="Alpha Pty Ltd", base_currency="AUD"))
        session.add(Company(name="Beta Pty Ltd", base_currency="AUD"))
        await session.commit()

        result = await session.execute(
            select(func.count()).select_from(Company).where(Company.archived_at.is_(None))
        )
        count = result.scalar_one()
        assert count >= 2
