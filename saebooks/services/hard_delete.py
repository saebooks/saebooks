"""Admin hard-delete helper — single source of truth for audit_log + DELETE.

Every JSON-API hard-delete route delegates here. We snapshot the row to
JSONB, insert one ``audit_log`` row, then physically delete the original
row in the same transaction. Caller commits.

Why a helper rather than a base class: the 20 affected routes are too
heterogeneous (different services, different version-locking shapes) to
share a base. A helper keeps the forensic-write logic in one place
without forcing every route to inherit a class hierarchy.

Sync-aware guard
----------------
Admins MUST be able to hard-delete synced objects (audit-log
immutability is intentionally overridden). However silently deleting a
Xero-linked invoice without warning the operator is unsafe — the row
still exists upstream and will resurrect on the next pull.
``check_sync_state_or_force`` enforces "if a ``sync_state`` row exists
for this object on any active connection, the caller must pass
``force=True`` (which carries the operator's explicit confirmation)".
The router supplies ``force`` from a
``X-Confirm-Hard-Delete-Synced: yes`` header.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.audit_log import AuditLog
from saebooks.models.user import User

# Object types that participate in sync — must match
# ``saebooks.models.sync.SyncObjectType`` membership.
_SYNCED_TABLES = {
    "contacts": "contact",
    "invoices": "invoice",
    "bills": "bill",
    "payments": "payment",
    "credit_notes": "credit_note",
    "journal_entries": "journal_entry",
}


class HardDeleteSyncedError(RuntimeError):
    """Raised when a hard-delete targets a synced object without ``force``.

    The router maps this to HTTP 409 with a body explaining the
    confirmation header. Carries the connection ID + provider for the
    UI to show "this row is linked to Xero — confirm to delete".
    """

    def __init__(
        self,
        *,
        table_name: str,
        row_id: str,
        connection_ids: list[str],
        providers: list[str],
    ) -> None:
        super().__init__(
            f"{table_name} row {row_id} is linked to {len(connection_ids)} "
            f"sync connection(s); pass force=True (X-Confirm-Hard-Delete-"
            f"Synced: yes) to override"
        )
        self.table_name = table_name
        self.row_id = row_id
        self.connection_ids = connection_ids
        self.providers = providers


async def check_sync_state_or_force(
    db: AsyncSession,
    obj: Any,
    *,
    table_name: str,
    force: bool,
) -> None:
    """Block hard-delete of synced rows unless ``force=True``.

    Looked up by ``(local_id == obj.id)`` across all sync_connections
    for the row's tenant. Returns silently when:

    * The table is not a sync-eligible table (e.g. ``users``,
      ``account_ranges``).
    * The row has no ``sync_state`` row (never synced).
    * ``force=True``.

    Raises ``HardDeleteSyncedError`` otherwise. Caller's transaction
    is unchanged on either path.
    """
    if force:
        return
    if table_name not in _SYNCED_TABLES:
        return
    # Local import to avoid a circular dep when sync.* imports User.
    from saebooks.models.sync import SyncConnection, SyncConnectionStatus, SyncState

    obj_id = getattr(obj, "id", None)
    if obj_id is None:
        return

    stmt = (
        select(SyncState, SyncConnection)
        .join(SyncConnection, SyncConnection.id == SyncState.connection_id)
        .where(
            SyncState.local_id == obj_id,
            SyncState.object_type == _SYNCED_TABLES[table_name],
            SyncConnection.status == SyncConnectionStatus.ACTIVE.value,
        )
    )
    rows = list((await db.execute(stmt)).all())
    if not rows:
        return
    raise HardDeleteSyncedError(
        table_name=table_name,
        row_id=str(obj_id),
        connection_ids=[str(c.id) for _s, c in rows],
        providers=[c.provider for _s, c in rows],
    )


def _snapshot(obj: Any) -> dict[str, Any]:
    """Return a JSON-safe dict of every column on the ORM row."""
    table = getattr(obj, "__table__", None)
    if table is None:
        raise TypeError(
            f"hard_delete needs an ORM row, got {type(obj).__name__} — "
            "under module delegation fetch the engine-local row "
            "(services.<record>.api_get_local), not the delegated schema object"
        )
    raw = {c.key: getattr(obj, c.key) for c in table.columns}
    return jsonable_encoder(raw)


async def hard_delete_with_audit(
    db: AsyncSession,
    obj: Any,
    table_name: str,
    current_user: User | None,
    reason: str | None = None,
    *,
    force_sync_override: bool = False,
) -> None:
    """Snapshot ``obj`` to audit_log, then physically delete it.

    The caller must already have verified the object belongs to the
    request's tenant. We pull tenant_id from ``obj`` when present and
    fall back to ``current_user.tenant_id`` otherwise (mirrors the
    invariant that every tenanted row carries the column).

    ``force_sync_override`` propagates the operator's explicit "yes,
    delete the synced row even though Xero still has it" header.
    Default ``False`` blocks the delete with ``HardDeleteSyncedError``
    when the row is sync-linked. Existing callers that don't pass this
    kwarg keep byte-identical behaviour on every table that isn't
    sync-eligible (see ``_SYNCED_TABLES``) — for sync-eligible tables
    they now get the guard's default (``force=False``), which is the
    intended new safety behaviour.

    Does NOT commit — the caller decides whether to commit or wrap in
    a larger transaction.
    """
    await check_sync_state_or_force(
        db, obj, table_name=table_name, force=force_sync_override,
    )
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
