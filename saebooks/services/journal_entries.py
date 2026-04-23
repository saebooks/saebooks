"""Journal entries service — API-oriented CRUD with optimistic locking.

This module provides the API surface for /api/v1/journal_entries.
It is intentionally separate from ``saebooks.services.journal`` (the
Jinja/legacy posting engine) so the two surfaces can evolve
independently.

Key design decisions:
- Optimistic locking via ``version`` INT + If-Match header.
- Every write appends a row to ``change_log``.
- ``void`` (DELETE in the REST API) is a soft-delete via ``archived_at``.
- Lines are always replaced in bulk on update (simpler than line-level diffs).
- ``tenant_id`` is required on every mutating call; extracted from auth.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import change_log as change_log_svc

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: JournalEntry) -> None:
        super().__init__(
            f"JournalEntry {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


class JournalEntryError(ValueError):
    """Raised on validation or business-rule failures."""


# ---------------------------------------------------------------------------
# Columns serialised into change_log.payload
# ---------------------------------------------------------------------------

_JE_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "ref",
    "entry_date",
    "description",
    "status",
    "posted_at",
    "posted_by",
    "reversal_of_id",
    "override_reason",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise(entry: JournalEntry) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in _JE_COLUMNS:
        val = getattr(entry, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, date):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_with_lines(session: AsyncSession, entry_id: uuid.UUID) -> JournalEntry | None:
    result = await session.execute(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.id == entry_id)
    )
    return result.scalar_one_or_none()


def _build_lines(entry_id: uuid.UUID, lines: list[dict[str, Any]]) -> list[JournalLine]:
    result = []
    for i, line_data in enumerate(lines, 1):
        result.append(
            JournalLine(
                entry_id=entry_id,
                line_no=i,
                account_id=line_data["account_id"],
                description=line_data.get("description"),
                debit=Decimal(str(line_data.get("debit", 0))),
                credit=Decimal(str(line_data.get("credit", 0))),
                tax_code_id=line_data.get("tax_code_id"),
                gst_amount=line_data.get("gst_amount"),
                project_id=line_data.get("project_id"),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    status: EntryStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[JournalEntry], int]:
    """Return (entries, total_count) — excludes archived/voided entries."""
    base_where = [
        JournalEntry.company_id == company_id,
        JournalEntry.archived_at.is_(None),
    ]
    if date_from is not None:
        base_where.append(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        base_where.append(JournalEntry.entry_date <= date_to)
    if status is not None:
        base_where.append(JournalEntry.status == status)

    count_stmt = (
        select(func.count())
        .select_from(JournalEntry)
        .where(*base_where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(*base_where)
        .order_by(JournalEntry.entry_date.desc(), JournalEntry.ref.desc())
        .limit(limit)
        .offset(offset)
    )
    entries = list((await session.execute(stmt)).scalars().unique().all())
    return entries, total


async def get(session: AsyncSession, entry_id: uuid.UUID) -> JournalEntry | None:
    """Fetch a single journal entry with its lines. Returns None if not found."""
    return await _get_with_lines(session, entry_id)


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    entry_date: date,
    narration: str | None = None,
    reference: str | None = None,
    lines: list[dict[str, Any]] | None = None,
) -> JournalEntry:
    """Create a journal entry (draft) with change_log row. version=1."""
    from saebooks.services.journal import next_ref  # avoid circular at module level

    ref = reference or await next_ref(session)

    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=entry_date,
        description=narration,
        status=EntryStatus.DRAFT,
        version=1,
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry)

    if lines:
        for line in _build_lines(entry.id, lines):
            session.add(line)

    await session.flush()
    # Re-fetch with lines loaded for serialisation
    entry = await _get_with_lines(session, entry.id)
    assert entry is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=entry.id,
        op="create",
        actor=actor,
        payload=_serialise(entry),
        version=entry.version,
    )
    await session.commit()
    return await _get_with_lines(session, entry.id)  # type: ignore[return-value]


async def update(
    session: AsyncSession,
    entry_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    entry_date: date | None = None,
    narration: str | None = None,
    reference: str | None = None,
    status: str | None = None,
    lines: list[dict[str, Any]] | None = None,
) -> JournalEntry:
    """Update a journal entry with optimistic locking + change_log."""
    entry = await _get_with_lines(session, entry_id)
    if entry is None:
        raise JournalEntryError(f"Journal entry {entry_id} not found")
    if entry.version != expected_version:
        raise VersionConflict(entry)

    if entry_date is not None:
        entry.entry_date = entry_date
    if narration is not None:
        entry.description = narration
    if reference is not None:
        entry.ref = reference
    if status is not None:
        entry.status = EntryStatus(status)

    if lines is not None:
        # Replace all lines
        for old_line in list(entry.lines):
            await session.delete(old_line)
        await session.flush()
        for line in _build_lines(entry.id, lines):
            session.add(line)

    entry.version = entry.version + 1
    await session.flush()
    await session.refresh(entry)

    # Re-fetch with lines
    entry = await _get_with_lines(session, entry_id)
    assert entry is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=entry.id,
        op="update",
        actor=actor,
        payload=_serialise(entry),
        version=entry.version,
    )
    await session.commit()
    return await _get_with_lines(session, entry_id)  # type: ignore[return-value]


async def void(
    session: AsyncSession,
    entry_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> JournalEntry:
    """Soft-delete (archive) a journal entry with optimistic locking + change_log."""
    entry = await _get_with_lines(session, entry_id)
    if entry is None:
        raise JournalEntryError(f"Journal entry {entry_id} not found")
    if entry.version != expected_version:
        raise VersionConflict(entry)

    entry.archived_at = datetime.now(UTC)
    entry.version = entry.version + 1
    await session.flush()
    await session.refresh(entry)

    entry = await _get_with_lines(session, entry_id)
    assert entry is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=entry.id,
        op="archive",
        actor=actor,
        payload=_serialise(entry),
        version=entry.version,
    )
    await session.commit()
    return await _get_with_lines(session, entry_id)  # type: ignore[return-value]
