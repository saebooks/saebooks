"""Append-only write log — Phase 0 scaffolding.

Every write through the saebooks.api.v1 surface (and every
legacy-Jinja write routed through the shared service layer) drops a
row into change_log so offline desktop clients can replay
server-side changes since a known cursor.

Rows are never mutated after insert. ChangeLog.id is the cursor —
monotonic BIGSERIAL, clients poll with
GET /api/v1/changes?since=<id> and advance.

Tenant isolation
----------------
Each row now carries tenant_id so RLS and the app-layer filter
both scope reads to the caller's tenant. The append() signature
accepts tenant_id as a keyword-only argument; every call site
that goes through saebooks.api.v1 already has the tenant on the
session (set by deps.get_session), and passes it here explicitly
for defence-in-depth.

Legacy call sites that were written before the tenant column existed
default to the placeholder UUID 00000000-0000-0000-0000-000000000001
which matches the backfill applied by migration 0118. They should be
updated to pass a real tenant when each service gains multi-tenant
awareness.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.change_log import ChangeLog

ChangeOp = Literal["create", "update", "archive"]

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def append(
    session: AsyncSession,
    *,
    entity: str,
    entity_id: uuid.UUID,
    op: ChangeOp,
    actor: str,
    payload: dict[str, Any],
    version: int,
    tenant_id: uuid.UUID = _DEFAULT_TENANT,
) -> ChangeLog:
    """Record one write. Does NOT commit — caller owns the transaction.

    Flushes so ChangeLog.id is populated on return (the API builds
    X-Cursor-Next from it).

    tenant_id must be the caller's tenant so RLS and the app-layer
    filter in since() both scope reads correctly. Defaults to the
    placeholder so legacy call sites keep working until they are
    updated.
    """
    # X-Dev-Skip-Audit short-circuit — see middleware/skip_audit.py.
    # Returns a no-op ChangeLog-like object the caller can ignore.
    from saebooks.services.dev_context import skip_audit_active
    if skip_audit_active():
        return ChangeLog(
            tenant_id=tenant_id,
            entity=entity,
            entity_id=entity_id,
            op=op,
            actor=actor + " [skip-audit]",
            payload={"note": "skip_audit_active"},
            version=version,
        )
    row = ChangeLog(
        tenant_id=tenant_id,
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
    tenant_id: uuid.UUID | None = None,
) -> list[ChangeLog]:
    """Return rows with id > cursor in ascending id order.

    When tenant_id is supplied, filters to rows owned by that
    tenant (app-layer defence-in-depth on top of RLS). When omitted,
    relies on RLS alone — which is sufficient for authenticated
    requests via deps.get_session but less readable for tests.
    """
    stmt = select(ChangeLog).where(ChangeLog.id > cursor)
    if tenant_id is not None:
        stmt = stmt.where(ChangeLog.tenant_id == tenant_id)
    if entity is not None:
        stmt = stmt.where(ChangeLog.entity == entity)
    stmt = stmt.order_by(ChangeLog.id).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
