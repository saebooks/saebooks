from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.settings import Setting


async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
    result = await session.execute(select(Setting.value).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row if row is not None else default


async def set(
    session: AsyncSession, key: str, value: Any, updated_by: str | None = None
) -> None:
    # Capture the prior value (if any) so we can record a before→after snapshot
    # of settings changes. Settings writes don't go through the ORM (they're a
    # Postgres upsert), so we snapshot manually via the low-level helper.
    existing = await session.execute(select(Setting).where(Setting.key == key))
    prior = existing.scalar_one_or_none()
    before = {"key": key, "value": prior.value} if prior is not None else None

    stmt = pg_insert(Setting).values(key=key, value=value, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": value, "updated_by": updated_by},
    )
    await session.execute(stmt)

    # Only snapshot if the value actually changed — and only for existing keys.
    # Creating a new key from scratch isn't really "risky".
    if prior is not None and prior.value != value:
        session.add(
            AuditSnapshot(
                table_name="settings",
                row_id=key,
                action="update",
                before_data=before,
                after_data={"key": key, "value": value},
                performed_by=updated_by,
            )
        )

    await session.commit()


async def all(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(Setting.key, Setting.value))
    return {key: value for key, value in result.all()}
