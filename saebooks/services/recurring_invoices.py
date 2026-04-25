"""Recurring invoice API service — CRUD + list.

API-tier functions for ``/api/v1/recurring_invoices``. A recurring invoice
is a template that carries schedule metadata and a set of lines; the
scheduler (out of scope here) uses it to mint real invoices.

This module is the CRUD/listing layer only — invoice spawning, next_run
recomputation on generation, and template-placeholder resolution are
separate concerns.

Status values (RecurrenceStatus): ACTIVE, PAUSED, ENDED.
Archive is a terminal state tracked via ``archived_at``; it is separate
from the ENDED lifecycle status.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.recurring_invoice import (
    RecurrenceFrequency,
    RecurrenceStatus,
    RecurringInvoice,
    RecurringInvoiceLine,
)
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Columns serialised into change_log.payload.
_RI_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "name",
    "frequency",
    "status",
    "anchor_day",
    "next_run",
    "end_date",
    "last_run",
    "due_days",
    "payment_terms",
    "notes",
    "auto_post",
    "invoices_generated",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RecurringInvoiceApiError(ValueError):
    """Raised on validation or state-transition failure (API tier)."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: RecurringInvoice) -> None:
        super().__init__(
            f"RecurringInvoice {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise(ri: RecurringInvoice) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _RI_COLUMNS:
        val = getattr(ri, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "isoformat"):  # date
            val = val.isoformat()
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Internal fetch helpers
# ---------------------------------------------------------------------------


async def _get_with_lines(
    session: AsyncSession, ri_id: uuid.UUID
) -> RecurringInvoice | None:
    result = await session.execute(
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(RecurringInvoice.id == ri_id)
    )
    return result.scalar_one_or_none()


def _build_lines(
    ri_id: uuid.UUID, lines: list[dict[str, Any]]
) -> list[RecurringInvoiceLine]:
    result = []
    for i, ld in enumerate(lines, 1):
        result.append(
            RecurringInvoiceLine(
                recurring_invoice_id=ri_id,
                line_no=i,
                description=ld["description"],
                account_id=uuid.UUID(str(ld["account_id"])),
                tax_code_id=(
                    uuid.UUID(str(ld["tax_code_id"]))
                    if ld.get("tax_code_id") is not None
                    else None
                ),
                quantity=Decimal(str(ld.get("quantity", "1"))),
                unit_price=Decimal(str(ld.get("unit_price", "0"))),
                discount_pct=Decimal(str(ld.get("discount_pct", "0"))),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_recurring_invoices(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    contact_id: str | None = None,
    frequency: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RecurringInvoice], int]:
    """Return (templates, total_count) filtered by status/contact/frequency/archived."""
    where = [RecurringInvoice.company_id == company_id]

    if not archived:
        where.append(RecurringInvoice.archived_at.is_(None))
    else:
        where.append(RecurringInvoice.archived_at.isnot(None))

    if status is not None:
        where.append(RecurringInvoice.status == status)

    if contact_id is not None:
        where.append(RecurringInvoice.contact_id == uuid.UUID(contact_id))

    if frequency is not None:
        where.append(RecurringInvoice.frequency == frequency)

    count_stmt = (
        select(sa_func.count())
        .select_from(RecurringInvoice)
        .where(*where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(*where)
        .order_by(RecurringInvoice.name)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().unique().all())
    return items, total


async def get(
    session: AsyncSession,
    ri_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> RecurringInvoice | None:
    """Fetch a single recurring invoice template with its lines.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    """
    if tenant_id is None:
        return await _get_with_lines(session, ri_id)
    result = await session.execute(
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(RecurringInvoice.id == ri_id, RecurringInvoice.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    contact_id: uuid.UUID,
    name: str,
    frequency: str,
    next_run: Any,
    status: str = "ACTIVE",
    anchor_day: int | None = None,
    end_date: Any | None = None,
    due_days: int = 30,
    payment_terms: str | None = None,
    notes: str | None = None,
    auto_post: bool = False,
    lines: list[dict[str, Any]] | None = None,
) -> RecurringInvoice:
    """Create a recurring invoice template with version=1 and change_log entry."""
    ri = RecurringInvoice(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        name=name.strip(),
        frequency=RecurrenceFrequency(frequency),
        status=RecurrenceStatus(status),
        anchor_day=anchor_day,
        next_run=next_run,
        end_date=end_date,
        due_days=due_days,
        payment_terms=payment_terms,
        notes=notes,
        auto_post=auto_post,
        version=1,
    )
    session.add(ri)
    await session.flush()
    await session.refresh(ri)

    if lines:
        for line in _build_lines(ri.id, lines):
            session.add(line)
        await session.flush()

    ri = await _get_with_lines(session, ri.id)
    assert ri is not None

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="created",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return await _get_with_lines(session, ri.id)  # type: ignore[return-value]


async def update(
    session: AsyncSession,
    ri_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    lines: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> RecurringInvoice:
    """Update a recurring invoice template with optimistic locking + change_log.

    If ``lines`` is present in the payload, existing lines are deleted and
    recreated from the supplied list (full-replace semantics). If ``lines`` is
    absent, existing lines are left untouched.
    """
    ri = await _get_with_lines(session, ri_id)
    if ri is None:
        raise RecurringInvoiceApiError(f"RecurringInvoice {ri_id} not found")
    if ri.version != expected_version:
        raise VersionConflict(ri)

    _ALLOWED_FIELDS = frozenset({
        "contact_id",
        "name",
        "frequency",
        "status",
        "anchor_day",
        "next_run",
        "end_date",
        "due_days",
        "payment_terms",
        "notes",
        "auto_post",
    })

    for key, value in kwargs.items():
        if key not in _ALLOWED_FIELDS:
            raise RecurringInvoiceApiError(f"Unknown or non-editable field: {key}")
        if key == "name" and value is not None:
            value = value.strip()
        if key == "frequency" and value is not None:
            value = RecurrenceFrequency(value)
        if key == "status" and value is not None:
            value = RecurrenceStatus(value)
        setattr(ri, key, value)

    # Lines full-replace if key is present.
    if lines is not None:
        for existing_line in list(ri.lines):
            await session.delete(existing_line)
        await session.flush()
        for line in _build_lines(ri.id, lines):
            session.add(line)

    ri.version = ri.version + 1
    await session.flush()
    await session.refresh(ri)

    # Re-fetch with lines for change_log serialisation.
    ri = await _get_with_lines(session, ri_id)
    assert ri is not None

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="updated",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return await _get_with_lines(session, ri_id)  # type: ignore[return-value]


async def delete(
    session: AsyncSession,
    ri_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> RecurringInvoice:
    """Soft-archive a recurring invoice template with optimistic locking + change_log.

    Archive is terminal — use PATCH status=PAUSED/ENDED for normal lifecycle.
    """
    ri = await _get_with_lines(session, ri_id)
    if ri is None:
        raise RecurringInvoiceApiError(f"RecurringInvoice {ri_id} not found")
    if ri.version != expected_version:
        raise VersionConflict(ri)

    ri.archived_at = datetime.now(UTC)
    ri.version = ri.version + 1
    await session.flush()
    await session.refresh(ri)

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="deleted",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return ri


async def pause(
    session: AsyncSession,
    ri_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> RecurringInvoice:
    """Transition ACTIVE → PAUSED with optimistic locking + change_log.

    Raises ``VersionConflict`` on stale version.
    Raises ``RecurringInvoiceApiError`` if not found or invalid transition.
    """
    ri = await _get_with_lines(session, ri_id)
    if ri is None:
        raise RecurringInvoiceApiError(f"RecurringInvoice {ri_id} not found")
    if ri.version != expected_version:
        raise VersionConflict(ri)
    if ri.status != RecurrenceStatus.ACTIVE:
        raise RecurringInvoiceApiError(
            f"Cannot pause: recurring invoice is in status {ri.status.value!r}, "
            "expected ACTIVE"
        )

    ri.status = RecurrenceStatus.PAUSED
    ri.version = ri.version + 1
    await session.flush()
    await session.refresh(ri)

    ri = await _get_with_lines(session, ri_id)
    assert ri is not None

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="paused",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return await _get_with_lines(session, ri_id)  # type: ignore[return-value]


async def resume(
    session: AsyncSession,
    ri_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> RecurringInvoice:
    """Transition PAUSED → ACTIVE with optimistic locking + change_log.

    Raises ``VersionConflict`` on stale version.
    Raises ``RecurringInvoiceApiError`` if not found or invalid transition.
    """
    ri = await _get_with_lines(session, ri_id)
    if ri is None:
        raise RecurringInvoiceApiError(f"RecurringInvoice {ri_id} not found")
    if ri.version != expected_version:
        raise VersionConflict(ri)
    if ri.status != RecurrenceStatus.PAUSED:
        raise RecurringInvoiceApiError(
            f"Cannot resume: recurring invoice is in status {ri.status.value!r}, "
            "expected PAUSED"
        )

    ri.status = RecurrenceStatus.ACTIVE
    ri.version = ri.version + 1
    await session.flush()
    await session.refresh(ri)

    ri = await _get_with_lines(session, ri_id)
    assert ri is not None

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="resumed",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return await _get_with_lines(session, ri_id)  # type: ignore[return-value]


async def end(
    session: AsyncSession,
    ri_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> RecurringInvoice:
    """Transition any non-ENDED status → ENDED with optimistic locking + change_log.

    ENDED is terminal — once ended, no further transitions are permitted.
    Raises ``VersionConflict`` on stale version.
    Raises ``RecurringInvoiceApiError`` if not found or already ENDED.
    """
    ri = await _get_with_lines(session, ri_id)
    if ri is None:
        raise RecurringInvoiceApiError(f"RecurringInvoice {ri_id} not found")
    if ri.version != expected_version:
        raise VersionConflict(ri)
    if ri.status == RecurrenceStatus.ENDED:
        raise RecurringInvoiceApiError(
            f"RecurringInvoice {ri_id} is already ENDED"
        )

    ri.status = RecurrenceStatus.ENDED
    ri.version = ri.version + 1
    await session.flush()
    await session.refresh(ri)

    ri = await _get_with_lines(session, ri_id)
    assert ri is not None

    await change_log_svc.append(
        session,
        entity="recurring_invoice",
        entity_id=ri.id,
        op="ended",
        actor=actor,
        payload=_serialise(ri),
        version=ri.version,
    )
    await session.commit()
    return await _get_with_lines(session, ri_id)  # type: ignore[return-value]


__all__ = [
    "RecurringInvoiceApiError",
    "VersionConflict",
    "create",
    "delete",
    "end",
    "get",
    "list_recurring_invoices",
    "pause",
    "resume",
    "update",
]
