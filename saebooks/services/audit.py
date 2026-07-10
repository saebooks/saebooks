"""Audit snapshot service — capture row state before risky edits.

Typical usage for an UPDATE:

    before = audit.capture(account)        # snapshot BEFORE mutation
    account.name = "new name"              # mutate
    # … any other changes …
    await audit.snapshot_row(              # snapshot AFTER, with before
        session, account,
        action="update",
        before_data=before,
        performed_by="web",
    )

For a DELETE (no after_data):

    await audit.snapshot_row(session, obj, action="delete", performed_by="web")
    await session.delete(obj)
"""
import csv
import enum
import io
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect
from sqlalchemy.types import Date, DateTime, Numeric
from sqlalchemy.types import Enum as SAEnum

from saebooks.models.audit_snapshot import AuditSnapshot


def _row_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize a SQLAlchemy model instance to a plain dict.

    Converts UUIDs/datetimes/enums/Decimals to JSON-safe forms so the
    result can go straight into a JSONB column.
    """
    mapper = inspect(type(obj))
    data: dict[str, Any] = {}
    for col in mapper.columns:
        val = getattr(obj, col.key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime | date):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # enums
            val = val.value
        data[col.key] = val
    return data


def capture(obj: Any) -> dict[str, Any]:
    """Freeze the current state of a SQLAlchemy row as a dict.

    Use this at the START of an update operation, before mutating `obj`.
    Then pass the result to `snapshot_row(..., before_data=...)` after
    the mutation has been applied.
    """
    return _row_to_dict(obj)


async def snapshot(
    session: AsyncSession,
    *,
    table_name: str,
    row_id: str,
    action: str,
    before_data: dict[str, Any],
    after_data: dict[str, Any] | None = None,
    reason: str | None = None,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> AuditSnapshot:
    """Create an audit snapshot record.

    ``tenant_id`` (Wave C RLS remediation, migration 0186): stamp the
    owning tenant at capture time so the row is visible under RLS to
    that tenant's ``saebooks_app`` session. ``None`` is a legitimate
    value for genuinely tenant-less captures (e.g. the global
    ``settings`` table has no tenant column at all) — the table's RLS
    policy treats a NULL ``tenant_id`` as "insertable, never
    SELECT-visible to any tenant" (see migration 0186's docstring), not
    an error. Prefer ``snapshot_row``'s auto-detection over passing this
    explicitly where possible.
    """
    snap = AuditSnapshot(
        table_name=table_name,
        row_id=row_id,
        action=action,
        before_data=before_data,
        after_data=after_data,
        reason=reason,
        performed_by=performed_by,
        tenant_id=tenant_id,
    )
    session.add(snap)
    await session.flush()
    return snap


async def snapshot_row(
    session: AsyncSession,
    obj: Any,
    *,
    action: str,
    before_data: dict[str, Any] | None = None,
    after_obj: Any | None = None,
    reason: str | None = None,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> AuditSnapshot:
    """Snapshot a SQLAlchemy model instance.

    The common patterns:

    * **Update**: pass a pre-captured `before_data` (from `capture()` called
      *before* mutation) and leave `obj` as the now-mutated instance —
      it will become `after_data`.
    * **Delete**: leave `before_data=None`; the current state of `obj` is
      captured as `before_data` and `after_data` is null.
    * **Create**: rarely useful — creates are easy to recover from the row
      itself. If you want one, pass the created row as `obj` with
      `action='create'`; current state becomes `before_data`, `after_data`
      is null.

    The `after_obj` kwarg is retained for back-compat: if given, its state
    goes into `after_data` regardless of other args.

    ``tenant_id`` (Wave C RLS remediation): every model captured by this
    codebase's 7 real ``audit_svc`` call sites (accounts/contacts/
    bank_rules/items/journal/projects/tax_codes) is a ``CompanyScoped``
    row that carries its own ``tenant_id`` column — so the default here
    auto-detects it via ``getattr(obj, "tenant_id", None)`` and callers
    need change NOTHING. Pass ``tenant_id`` explicitly only when ``obj``
    itself has no such column (e.g. ``JournalLine`` — a child table with
    no tenant_id of its own, 0055's carve-out; the caller has the
    parent entry's tenant_id in scope and should pass it through). A
    resolved ``None`` (no attribute, no explicit override) is a
    legitimate outcome for a genuinely tenant-less capture, not an
    error — see ``snapshot``'s docstring.
    """
    mapper = inspect(type(obj))
    table_name = mapper.mapped_table.name
    pk_cols = [col.key for col in mapper.primary_key]
    row_id = str(getattr(obj, pk_cols[0]))
    resolved_tenant_id = tenant_id if tenant_id is not None else getattr(obj, "tenant_id", None)

    if before_data is not None:
        # Caller captured before-state explicitly; obj is the after state.
        before = before_data
        after = _row_to_dict(after_obj) if after_obj is not None else _row_to_dict(obj)
    else:
        # Delete/create semantics: obj IS the before, no after.
        before = _row_to_dict(obj)
        after = _row_to_dict(after_obj) if after_obj is not None else None

    return await snapshot(
        session,
        table_name=table_name,
        row_id=row_id,
        action=action,
        before_data=before,
        after_data=after,
        reason=reason,
        performed_by=performed_by,
        tenant_id=resolved_tenant_id,
    )


async def list_snapshots(
    session: AsyncSession,
    table_name: str,
    row_id: str,
    *,
    limit: int = 50,
    tenant_id: uuid.UUID | None = None,
) -> list[AuditSnapshot]:
    """Get snapshot history for a specific row.

    ``tenant_id`` (Wave C): application-level tenant filter, belt-and-
    braces on top of FORCE RLS (migration 0186) — mirrors the pattern
    ``api/v1/admin.py``'s ``get_audit_log`` uses for ``change_log``.
    ``None`` (the default) preserves the pre-Wave-C unscoped behaviour
    for internal/CLI callers that already run under a single-tenant
    connection; the new gated browse API always passes it.
    """
    stmt = select(AuditSnapshot).where(
        AuditSnapshot.table_name == table_name,
        AuditSnapshot.row_id == row_id,
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditSnapshot.tenant_id == tenant_id)
    stmt = stmt.order_by(AuditSnapshot.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def browse(
    session: AsyncSession,
    *,
    table_name: str | None = None,
    row_id: str | None = None,
    action: str | None = None,
    performed_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
    tenant_id: uuid.UUID | None = None,
) -> list[AuditSnapshot]:
    """Browse snapshots with optional filters — for the audit viewer.

    ``tenant_id`` (Wave C): see ``list_snapshots``'s docstring — same
    belt-and-braces app-level filter, ``None`` preserves prior
    unscoped behaviour for internal callers.
    """
    stmt = select(AuditSnapshot)
    if table_name:
        stmt = stmt.where(AuditSnapshot.table_name == table_name)
    if row_id:
        stmt = stmt.where(AuditSnapshot.row_id == row_id)
    if action:
        stmt = stmt.where(AuditSnapshot.action == action)
    if performed_by:
        stmt = stmt.where(AuditSnapshot.performed_by == performed_by)
    if tenant_id is not None:
        stmt = stmt.where(AuditSnapshot.tenant_id == tenant_id)
    stmt = stmt.order_by(AuditSnapshot.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_browse(
    session: AsyncSession,
    *,
    table_name: str | None = None,
    row_id: str | None = None,
    action: str | None = None,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Count rows matching ``browse``'s filters — for the browse API's
    pagination total (same filter set, no limit/offset)."""
    from sqlalchemy import func as _func

    stmt = select(_func.count()).select_from(AuditSnapshot)
    if table_name:
        stmt = stmt.where(AuditSnapshot.table_name == table_name)
    if row_id:
        stmt = stmt.where(AuditSnapshot.row_id == row_id)
    if action:
        stmt = stmt.where(AuditSnapshot.action == action)
    if performed_by:
        stmt = stmt.where(AuditSnapshot.performed_by == performed_by)
    if tenant_id is not None:
        stmt = stmt.where(AuditSnapshot.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_snapshot(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> AuditSnapshot | None:
    """Fetch one snapshot by id.

    ``tenant_id`` (Wave C): when supplied, a foreign-tenant id returns
    ``None`` (treated as not-found) even if the row exists — same
    belt-and-braces shape as ``journal.get``'s tenant-scoped lookup.
    """
    snap = await session.get(AuditSnapshot, snapshot_id)
    if snap is None:
        return None
    if tenant_id is not None and snap.tenant_id != tenant_id:
        return None
    return snap


async def distinct_tables(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """Distinct table names that have any audit snapshots — for the filter dropdown."""
    stmt = select(AuditSnapshot.table_name).distinct()
    if tenant_id is not None:
        stmt = stmt.where(AuditSnapshot.tenant_id == tenant_id)
    stmt = stmt.order_by(AuditSnapshot.table_name)
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


async def distinct_actors(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """Distinct `performed_by` values — for the filter dropdown."""
    stmt = select(AuditSnapshot.performed_by).where(
        AuditSnapshot.performed_by.is_not(None)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditSnapshot.tenant_id == tenant_id)
    stmt = stmt.distinct().order_by(AuditSnapshot.performed_by)
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


# ---------------------------------------------------------------------------
# Revert — apply a snapshot's before-state back to the live row
# ---------------------------------------------------------------------------

# Actions that can be reverted. `migrate`, `reverse`, `delete` need more
# complex handling (reviving deleted rows with intact FKs, undoing side-effect
# rows, etc.) — not implemented in v1.
REVERTABLE_ACTIONS = {"update", "archive"}


def _resolve_model(table_name: str) -> Any | None:
    """Find the SQLAlchemy model class mapped to `table_name`, or None."""
    # Import locally to avoid circular import at module load time.
    from saebooks.models import (
        Account,
        AccountRange,
        BankRule,
        BankStatementLine,
        Company,
        Contact,
        JournalEntry,
        JournalLine,
        JournalTemplate,
        PeriodLock,
        Setting,
        TaxCode,
    )
    registry: dict[str, Any] = {
        "accounts": Account,
        "account_ranges": AccountRange,
        "bank_rules": BankRule,
        "bank_statement_lines": BankStatementLine,
        "companies": Company,
        "contacts": Contact,
        "journal_entries": JournalEntry,
        "journal_lines": JournalLine,
        "journal_templates": JournalTemplate,
        "period_locks": PeriodLock,
        "settings": Setting,
        "tax_codes": TaxCode,
    }
    return registry.get(table_name)


def _coerce_from_jsonb(column: Any, value: Any) -> Any:
    """Convert a JSONB-serialised value back to the Python type the column expects.

    The serialiser in `_row_to_dict` turns UUIDs/datetimes/dates/Decimals/enums
    into strings so they fit in JSONB. Reverting means reversing that coercion
    so SQLAlchemy accepts the value on the model.
    """
    if value is None:
        return None
    col_type = column.type
    # UUIDs (from postgresql.UUID(as_uuid=True))
    if isinstance(col_type, PG_UUID):
        return uuid.UUID(value) if isinstance(value, str) else value
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(col_type, Date):
        return date.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(col_type, Numeric):
        return Decimal(value) if not isinstance(value, Decimal) else value
    if isinstance(col_type, SAEnum):
        # Enum: resolve by value
        py_enum = col_type.enum_class
        if py_enum is not None and not isinstance(value, enum.Enum):
            # enum_class may represent values by name or by value — try both
            for member in py_enum:
                if member.value == value or member.name == value:
                    return member
        return value
    return value


class RevertError(Exception):
    """Raised when a snapshot can't be reverted."""


async def revert(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> AuditSnapshot:
    """Apply a snapshot's `before_data` back to the live row.

    Only supports `update` and `archive` actions — delete/migrate/reverse are
    too lossy to safely undo (dependent rows may have moved or been purged).
    The row must still exist; if it's been deleted since the snapshot was
    taken, use a restore instead.

    Writes a new snapshot recording the revert itself, so the audit trail
    captures both the original mutation AND the revert.
    """
    snap = await session.get(AuditSnapshot, snapshot_id)
    if snap is None:
        raise RevertError(f"Snapshot {snapshot_id} not found")
    if snap.action not in REVERTABLE_ACTIONS:
        raise RevertError(
            f"Cannot revert action '{snap.action}' — only "
            f"{sorted(REVERTABLE_ACTIONS)} are supported."
        )
    if not snap.before_data:
        raise RevertError("Snapshot has no before_data to restore from.")

    # Settings are a special case — they're keyed by string, not UUID, and
    # go through the settings service's upsert path.
    if snap.table_name == "settings":
        # Deferred import to avoid a module-load cycle
        from saebooks.services import settings as settings_svc
        key = snap.row_id
        value = snap.before_data.get("value")
        await settings_svc.set(
            session, key, value, updated_by=performed_by or "revert"
        )
        # settings.set() has already written its own update snapshot,
        # but we want a marker linking back to the original — re-fetch
        # the one it just wrote and tag the reason.
        latest = await session.execute(
            select(AuditSnapshot)
            .where(
                AuditSnapshot.table_name == "settings",
                AuditSnapshot.row_id == key,
            )
            .order_by(AuditSnapshot.created_at.desc())
            .limit(1)
        )
        marker = latest.scalar_one_or_none()
        if marker is not None:
            marker.reason = f"Revert of snapshot {snap.id}"
            await session.commit()
        return marker or snap

    model = _resolve_model(snap.table_name)
    if model is None:
        raise RevertError(f"No model registered for table '{snap.table_name}'")

    mapper = inspect(model)
    pk_cols = list(mapper.primary_key)
    if len(pk_cols) != 1:
        raise RevertError(
            f"Revert requires a single-column primary key on '{snap.table_name}'"
        )
    pk_col = pk_cols[0]
    row_pk: Any = snap.row_id
    if isinstance(pk_col.type, PG_UUID):
        row_pk = uuid.UUID(snap.row_id)

    obj = await session.get(model, row_pk)
    if obj is None:
        raise RevertError(
            f"Row {snap.row_id} on {snap.table_name} no longer exists — "
            "can't revert (was it deleted?)."
        )

    # Capture the current post-mutation state, then write back the before_data
    # fields. Skip identity and metadata columns — we never overwrite those.
    before_current = capture(obj)
    skip_cols = {"id", "created_at", "updated_at"}
    for col in mapper.columns:
        if col.key in skip_cols:
            continue
        if col.key not in snap.before_data:
            continue
        raw = snap.before_data[col.key]
        setattr(obj, col.key, _coerce_from_jsonb(col, raw))

    # Record the revert itself so the audit trail is self-describing.
    await snapshot_row(
        session, obj,
        action="update",
        before_data=before_current,
        reason=f"Revert of snapshot {snap.id}",
        performed_by=performed_by,
    )
    await session.commit()
    return snap


# ---------------------------------------------------------------------------
# CSV export — 5-year retention compliance dump
# ---------------------------------------------------------------------------


EXPORT_COLUMNS = (
    "id",
    "created_at",
    "table_name",
    "row_id",
    "action",
    "performed_by",
    "reason",
    "before_data",
    "after_data",
)


async def export_csv(
    session: AsyncSession,
    *,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    table_name: str | None = None,
    performed_by: str | None = None,
) -> str:
    """Export audit snapshots as a CSV string.

    Used by the 5-year-retention admin exporter. Rows are ordered
    ``created_at ASC`` so the exported file reads chronologically.
    JSONB columns are JSON-serialised so the CSV round-trips — a
    reviewer can re-load the export into jq / pandas without a
    second parser.

    ``from_date`` / ``to_date`` are inclusive on both ends (common
    sense for accounting: "give me FY25" means 1-Jul-2024 through
    30-Jun-2025, both days included).
    """
    stmt = select(AuditSnapshot)
    if from_date is not None:
        stmt = stmt.where(AuditSnapshot.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(AuditSnapshot.created_at <= to_date)
    if table_name:
        stmt = stmt.where(AuditSnapshot.table_name == table_name)
    if performed_by:
        stmt = stmt.where(AuditSnapshot.performed_by == performed_by)
    stmt = stmt.order_by(AuditSnapshot.created_at.asc())
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                str(r.id),
                r.created_at.isoformat() if r.created_at else "",
                r.table_name,
                r.row_id,
                r.action,
                r.performed_by or "",
                r.reason or "",
                json.dumps(r.before_data, sort_keys=True) if r.before_data else "",
                json.dumps(r.after_data, sort_keys=True) if r.after_data else "",
            ]
        )
    return buf.getvalue()


async def count_snapshots(
    session: AsyncSession,
    *,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    table_name: str | None = None,
    performed_by: str | None = None,
) -> int:
    """Count snapshots matching the same filters as ``export_csv``.

    Lets the admin UI show "N rows will be exported" before the click.
    """
    from sqlalchemy import func as _func

    stmt = select(_func.count()).select_from(AuditSnapshot)
    if from_date is not None:
        stmt = stmt.where(AuditSnapshot.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(AuditSnapshot.created_at <= to_date)
    if table_name:
        stmt = stmt.where(AuditSnapshot.table_name == table_name)
    if performed_by:
        stmt = stmt.where(AuditSnapshot.performed_by == performed_by)
    result = await session.execute(stmt)
    return int(result.scalar_one())


def diff_fields(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[tuple[str, Any, Any]]:
    """Return list of (field, before_val, after_val) for fields that changed.

    - If `after` is None (delete action), every non-metadata field is shown
      as (before, None).
    - If `before` is None (shouldn't happen in practice), shown as (None, after).
    - Metadata fields (created_at, updated_at) are excluded from the diff.
    """
    ignore = {"created_at", "updated_at"}
    out: list[tuple[str, Any, Any]] = []
    if after is None:
        if before is None:
            return out
        for k, v in before.items():
            if k in ignore:
                continue
            out.append((k, v, None))
        return out
    if before is None:
        for k, v in after.items():
            if k in ignore:
                continue
            out.append((k, None, v))
        return out

    all_keys = set(before.keys()) | set(after.keys())
    for k in sorted(all_keys):
        if k in ignore:
            continue
        b = before.get(k)
        a = after.get(k)
        if b != a:
            out.append((k, b, a))
    return out
