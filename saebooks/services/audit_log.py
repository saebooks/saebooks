"""Attributable audit-log writer for compliance hot-path events (C2).

The ``audit_log`` table (see ``saebooks.models.audit_log.AuditLog``) records
*who* did *what* to *which row*, with a JSONB snapshot of the row at the
moment of the action and an optional operator-supplied ``reason``. It is the
ATO-defensible "who changed this and when" trail.

Design decisions (see docs/design/audit-log-coverage.md):

* **Option B — text column + Python enum.** ``audit_log.action`` stays
  ``TEXT``; the canonical action vocabulary is enforced at the application
  layer by ``AuditAction`` below, mirroring how the other status columns are
  app-enforced. New actions land without a migration.

* **``append()`` does NOT flush or commit.** It only ``session.add()``s the
  row. The caller's existing commit flushes the action and the audit row
  together, so the audit row shares the action's transaction:

    - action commits  -> audit row commits with it
    - action rolls back (e.g. a period-lock ``PostingError``) -> the audit
      row rolls back too. No orphan audit rows, no audit rows for actions
      that never happened.

* **Actor is a real user UUID**, never the JWT prefix. The caller threads
  ``actor_user_id`` down from the ``get_active_user_id`` dependency.
"""
from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.audit_log import AuditLog


class AuditAction(StrEnum):
    """Canonical vocabulary for ``audit_log.action``.

    Text column + Python enum (design Option B): typo-safe at the call site
    without a DB migration per new action. Values are dotted
    ``<entity>.<verb>`` strings, matching the design table.
    """

    # Hard-delete forensics (pre-existing writer in services/hard_delete.py).
    HARD_DELETE = "hard_delete"

    # Hot-path POST / VOID transitions (C2 — this work).
    INVOICE_POST = "invoice.post"
    INVOICE_VOID = "invoice.void"
    BILL_POST = "bill.post"
    BILL_VOID = "bill.void"
    PAYMENT_POST = "payment.post"
    PAYMENT_VOID = "payment.void"
    CREDIT_NOTE_POST = "credit_note.post"
    # Money-in record types (0157).
    SUPPLIER_CREDIT_NOTE_POST = "supplier_credit_note.post"
    SUPPLIER_CREDIT_NOTE_VOID = "supplier_credit_note.void"
    RECEIPT_POST = "receipt.post"
    RECEIPT_VOID = "receipt.void"

    # Journal entry posted into a locked period via an authorised override.
    JOURNAL_OVERRIDE_POST = "journal.override_post"

    # Period-lock forensics — an admin removed a lock row independent of
    # year-end close (M3b period-locks CRUD).
    PERIOD_LOCK_DELETE = "period_lock.delete"


async def append(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,
    table_name: str,
    row_id: str,
    row_snapshot: dict[str, Any],
    reason: str | None = None,
) -> None:
    """Stage one ``audit_log`` row on ``session``.

    Critically: **no flush, no commit.** The caller owns the transaction —
    its commit binds this audit row to the same transaction as the action,
    giving the in-transaction guarantee documented at module level.

    Parameters
    ----------
    tenant_id
        Owning tenant. Must match the acting row's tenant; under FORCE RLS
        the ``tenant_isolation`` WITH CHECK rejects a cross-tenant insert.
    actor_user_id
        The acting user's UUID (from ``get_active_user_id``). Never the JWT
        prefix.
    action
        An ``AuditAction`` value (a ``str`` subclass).
    table_name, row_id
        The physical table and the affected row's id (stringified).
    row_snapshot
        JSON-serialisable dict of the row at the point of action (the Out
        shape / change_log payload). Must be non-empty.
    reason
        Optional operator-supplied rationale (void reason, override reason).
        ``None`` for routine posts.
    """
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=str(action),
            table_name=table_name,
            row_id=str(row_id),
            row_snapshot=row_snapshot,
            reason=reason,
        )
    )
    # No flush here — the caller's commit flushes the action + audit together.
