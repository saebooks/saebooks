import uuid

from sqlalchemy import delete, func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company


async def test_schema_permits_multiple_companies() -> None:
    tag = uuid.uuid4().hex[:8]
    a_name = f"Alpha-{tag}"
    b_name = f"Beta-{tag}"
    async with AsyncSessionLocal() as session:
        session.add(Company(name=a_name, base_currency="AUD"))
        session.add(Company(name=b_name, base_currency="AUD"))
        await session.commit()

        result = await session.execute(
            select(func.count())
            .select_from(Company)
            .where(Company.name.in_((a_name, b_name)))
        )
        assert result.scalar_one() == 2

        await session.execute(delete(Company).where(Company.name.in_((a_name, b_name))))
        await session.commit()
