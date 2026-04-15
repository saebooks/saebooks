from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.settings import Setting


async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
    result = await session.execute(select(Setting.value).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row if row is not None else default


async def set(
    session: AsyncSession, key: str, value: Any, updated_by: str | None = None
) -> None:
    stmt = pg_insert(Setting).values(key=key, value=value, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": value, "updated_by": updated_by},
    )
    await session.execute(stmt)
    await session.commit()


async def all(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(Setting.key, Setting.value))
    return {key: value for key, value in result.all()}
