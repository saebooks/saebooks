"""Admin hard-delete helper — single source of truth for audit_log + DELETE.

Every JSON-API hard-delete route delegates here. We snapshot the row to
JSONB, insert one ``audit_log`` row, then physically delete the original
row in the same transaction. Caller commits.

Why a helper rather than a base class: the 20 affected routes are too
heterogeneous (different services, different version-locking shapes) to
share a base. A helper keeps the forensic-write logic in one place
without forcing every route to inherit a class hierarchy.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.audit_log import AuditLog
from saebooks.models.user import User


def _snapshot(obj: Any) -> dict[str, Any]:
    """Return a JSON-safe dict of every column on the ORM row."""
    raw = {c.key: getattr(obj, c.key) for c in obj.__table__.columns}
    return jsonable_encoder(raw)


async def hard_delete_with_audit(
    db: AsyncSession,
    obj: Any,
    table_name: str,
    current_user: User | None,
    reason: str | None = None,
) -> None:
    """Snapshot ``obj`` to audit_log, then physically delete it.

    The caller must already have verified the object belongs to the
    request's tenant. We pull tenant_id from ``obj`` when present and
    fall back to ``current_user.tenant_id`` otherwise (mirrors the
    invariant that every tenanted row carries the column).

    Does NOT commit — the caller decides whether to commit or wrap in
    a larger transaction.
    """
    snapshot = _snapshot(obj)
    tenant_id = getattr(obj, "tenant_id", None)
    if tenant_id is None and current_user is not None:
        tenant_id = current_user.tenant_id
    actor = current_user.id if current_user is not None else uuid.UUID(int=0)
    row = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor,
        action="hard_delete",
        table_name=table_name,
        row_id=str(getattr(obj, "id", "")),
        row_snapshot=snapshot,
        reason=reason,
    )
    db.add(row)
    await db.delete(obj)
    await db.flush()
