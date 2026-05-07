"""Append-only write log — Phase 0 scaffolding.

Every write through the ``saebooks.api.v1`` surface (and every
legacy-Jinja write routed through the shared service layer) drops a
row into ``change_log`` so offline desktop clients can replay
server-side changes since a known cursor.

Rows are never mutated after insert. ``ChangeLog.id`` is the cursor —
monotonic BIGSERIAL, clients poll with
``GET /api/v1/changes?since=<id>`` and advance.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.change_log import ChangeLog

ChangeOp = Literal["create", "update", "archive"]


async def append(
    session: AsyncSession,
    *,
    entity: str,
    entity_id: uuid.UUID,
    op: ChangeOp,
    actor: str,
    payload: dict[str, Any],
    version: int,
) -> ChangeLog:
    """Record one write. Does NOT commit — caller owns the transaction.

    Flushes so ``ChangeLog.id`` is populated on return (the API builds
    ``X-Cursor-Next`` from it).
    """
    row = ChangeLog(
        entity=entity,
        entity_id=entity_id,
        op=op,
        actor=actor,
        payload=payload,
        version=version,
    )
    session.add(row)
    await session.flush()
    return row


async def since(
    session: AsyncSession,
    *,
    cursor: int,
    limit: int,
    entity: str | None = None,
) -> list[ChangeLog]:
    """Return rows with ``id > cursor`` in ascending id order."""
    stmt = select(ChangeLog).where(ChangeLog.id > cursor)
    if entity is not None:
        stmt = stmt.where(ChangeLog.entity == entity)
    stmt = stmt.order_by(ChangeLog.id).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
