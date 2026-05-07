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

from saebooks.models.account import Account
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


async def get(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry | None:
    """Fetch a single journal entry with its lines. Returns None if not found.

    P0 cross-tenant leak fix: when ``tenant_id`` is supplied, the
    lookup is filtered by tenant — a foreign-tenant id returns
    ``None`` even if the row exists. The parameter is keyword-only
    and optional so existing internal callers keep working unchanged;
    the API layer always supplies it.
    """
    if tenant_id is None:
        return await _get_with_lines(session, entry_id)
    result = await session.execute(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(
            JournalEntry.id == entry_id,
            JournalEntry.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _validate_accounts_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    lines: list[dict[str, Any]],
) -> None:
    """Raise JournalEntryError if any line account belongs to a different tenant."""
    if not lines:
        return
    ids = [uuid.UUID(str(ln["account_id"])) for ln in lines]
    result = await session.execute(
        select(Account.id, Account.tenant_id).where(Account.id.in_(ids))
    )
    rows = {r.id: r.tenant_id for r in result.all()}
    bad = [i for i in ids if rows.get(i) != tenant_id]
    if bad:
        raise JournalEntryError(
            "Account(s) do not belong to this tenant: "
            + ", ".join(str(i) for i in bad)
        )


async def _validate_lines_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[dict[str, Any]],
) -> None:
    """Raise JournalEntryError when any line account or tax_code belongs to a different company."""
    from saebooks.models.tax_code import TaxCode  # local to avoid circular at module level

    if not lines:
        return
    for ln in lines:
        acct_id = uuid.UUID(str(ln["account_id"]))
        result = await session.execute(
            select(Account.id).where(
                Account.id == acct_id, Account.company_id == company_id
            )
        )
        if result.scalar_one_or_none() is None:
            raise JournalEntryError(f"account {acct_id} not found")
        tc_raw = ln.get("tax_code_id")
        if tc_raw:
            tc_id = uuid.UUID(str(tc_raw))
            tc_result = await session.execute(
                select(TaxCode.id).where(
                    TaxCode.id == tc_id, TaxCode.company_id == company_id
                )
            )
            if tc_result.scalar_one_or_none() is None:
                raise JournalEntryError(f"tax_code {tc_id} not found")


def _assert_lines_balanced(lines: list[dict[str, Any]], ref: str = "entry") -> None:
    """Raise JournalEntryError if the lines list is not debit-credit balanced.

    This is the service-layer defence-in-depth guard.  The Pydantic schema
    validator in ``JournalEntryCreate`` / ``JournalEntryUpdate`` is the first
    layer and returns 422 before we ever reach the service.  This guard
    protects callers who bypass the schema (e.g. internal tooling, migrations,
    future gRPC endpoints).
    """
    if not lines:
        return
    total_debit = sum(Decimal(str(ln.get("debit", 0))) for ln in lines)
    total_credit = sum(Decimal(str(ln.get("credit", 0))) for ln in lines)
    if total_debit != total_credit:
        raise JournalEntryError(
            f"Journal entry {ref} lines are unbalanced: "
            f"debits={total_debit}, credits={total_credit}"
        )


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

    if lines:
        _assert_lines_balanced(lines, reference or "(pending-ref)")
        await _validate_accounts_tenant(session, tenant_id, lines)
        await _validate_lines_company(session, company_id, lines)

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
        _assert_lines_balanced(lines, entry.ref)
        await _validate_accounts_tenant(session, entry.tenant_id, lines)
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


async def api_post(
    session: AsyncSession,
    entry_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    override_reason: str | None = None,
) -> JournalEntry:
    """Transition DRAFT → POSTED with optimistic locking + change_log.

    Delegates to ``services.journal.post()`` which checks period locks,
    auto-posts GST lines, and verifies balance. After that completes, we
    bump ``version`` and append a change_log row.

    ``override_reason`` is passed through to the period-lock check — when
    non-empty it bypasses the lock and is stored on the entry for audit.
    """
    from saebooks.services import journal as journal_svc  # avoid circular at module level

    entry = await _get_with_lines(session, entry_id)
    if entry is None:
        raise JournalEntryError(f"Journal entry {entry_id} not found")
    if entry.version != expected_version:
        raise VersionConflict(entry)
    if entry.status == EntryStatus.POSTED:
        raise JournalEntryError(
            f"Journal entry {entry.ref} is already POSTED"
        )
    if entry.status == EntryStatus.REVERSED:
        raise JournalEntryError(
            f"Journal entry {entry.ref} is REVERSED and cannot be re-posted"
        )

    # Delegate to legacy journal service (checks period lock, GST, balance, commits).
    # PostingError (period lock, trust commingling, balance) is a legacy exception
    # type unknown to this module's router; translate it to JournalEntryError so the
    # router's existing except clause returns 422 instead of propagating a 500.
    try:
        entry = await journal_svc.post(
            session, entry_id, posted_by=actor, override_reason=override_reason
        )
    except journal_svc.PostingError as exc:
        raise JournalEntryError(str(exc)) from exc

    # Bump version + append change_log in a second transaction.
    entry.version = entry.version + 1
    await session.flush()
    await session.refresh(entry)

    entry = await _get_with_lines(session, entry_id)
    assert entry is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=entry.id,
        op="posted",
        actor=actor,
        payload=_serialise(entry),
        version=entry.version,
    )
    await session.commit()
    return await _get_with_lines(session, entry_id)  # type: ignore[return-value]


async def api_reverse(
    session: AsyncSession,
    entry_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> JournalEntry:
    """Create a reversal of a POSTED journal entry.

    Fetches the original entry, verifies it is POSTED and the version
    matches, then delegates to ``services.journal.reverse()`` which:
    - creates a new JournalEntry with debit/credit swapped
    - auto-posts the reversal
    - marks the original as REVERSED

    Returns the new reversal entry. The original's status change is
    committed inside ``journal_svc.reverse()``.

    After the legacy pipeline commits we bump ``version`` on the original
    and append change_log rows for both the reversal creation and the
    original's REVERSED transition.
    """
    from saebooks.services import journal as journal_svc  # avoid circular at module level

    entry = await _get_with_lines(session, entry_id)
    if entry is None:
        raise JournalEntryError(f"Journal entry {entry_id} not found")
    if entry.version != expected_version:
        raise VersionConflict(entry)
    if entry.status != EntryStatus.POSTED:
        raise JournalEntryError(
            f"Journal entry {entry.ref} must be POSTED to reverse "
            f"(current status: {entry.status})"
        )

    # Delegate to legacy pipeline — creates reversal JE, posts it, marks
    # original REVERSED, and commits. Returns the new reversal entry.
    reversal = await journal_svc.reverse(session, entry_id, posted_by=actor)

    # Re-fetch the original (now REVERSED) and bump its version so that
    # callers get a consistent If-Match token after the transition.
    original = await _get_with_lines(session, entry_id)
    assert original is not None
    original.version = original.version + 1
    await session.flush()
    await session.refresh(original)
    original = await _get_with_lines(session, entry_id)
    assert original is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=original.id,
        op="reversed",
        actor=actor,
        payload=_serialise(original),
        version=original.version,
    )

    # Also log the new reversal entry as a create.
    reversal_loaded = await _get_with_lines(session, reversal.id)
    assert reversal_loaded is not None

    await change_log_svc.append(
        session,
        entity="journal_entry",
        entity_id=reversal_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise(reversal_loaded),
        version=reversal_loaded.version,
    )

    await session.commit()
    return await _get_with_lines(session, reversal.id)  # type: ignore[return-value]
