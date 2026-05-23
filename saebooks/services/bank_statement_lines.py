"""Bank statement line service — CRUD for ``bank_statement_lines``.

Design: ``BankStatementLine`` rows are filtered by ``account_id`` (a bank
account — ``accounts`` row where ``bsb IS NOT NULL``).  Each line has an
``amount`` (positive = deposit/inflow, negative = withdrawal/outflow),
an optional ``balance`` (running balance after the line), ``txn_date``,
``description``, ``reference``, and a ``status``
(UNMATCHED / MATCHED / IGNORED).

Optimistic locking, change_log, and tenant scoping follow the same
conventions as every other API-tier service.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Columns serialised into change_log.payload.
_BSL_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "account_id",
    "txn_date",
    "description",
    "amount",
    "balance",
    "reference",
    "status",
    "matched_entry_id",
    "matched_at",
    "matched_by",
    "matched_to_type",
    "matched_to_id",
    "contact_id",
    "bank_rule_id",
    "bank_feed_account_id",
    "external_id",
    "version",
    "created_at",
    "archived_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BankStatementLineError(ValueError):
    """Raised on bank statement line validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: BankStatementLine) -> None:
        super().__init__(
            f"BankStatementLine {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialise(line: BankStatementLine) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _BSL_COLUMNS:
        val = getattr(line, key, None)
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


def _base_filter(
    company_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    status: StatementLineStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_archived: bool = False,
):
    """Build list of WHERE conditions for bank statement line queries."""
    conditions = [BankStatementLine.company_id == company_id]
    if not include_archived:
        conditions.append(BankStatementLine.archived_at.is_(None))
    if account_id is not None:
        conditions.append(BankStatementLine.account_id == account_id)
    if status is not None:
        conditions.append(BankStatementLine.status == status)
    if date_from is not None:
        conditions.append(BankStatementLine.txn_date >= date_from)
    if date_to is not None:
        conditions.append(BankStatementLine.txn_date <= date_to)
    return conditions


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


# Sortable columns: API string → SQLAlchemy column.
_SORT_COLUMNS = {
    "date": BankStatementLine.txn_date,
    "description": BankStatementLine.description,
    "amount": BankStatementLine.amount,
    "balance": BankStatementLine.balance,
    "status": BankStatementLine.status,
    "reference": BankStatementLine.reference,
}


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    status: StatementLineStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = "date",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BankStatementLine], int]:
    """Return (lines, total_count) — active (non-archived) only."""
    where = _base_filter(
        company_id,
        account_id=account_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )

    count_stmt = (
        select(sa_func.count()).select_from(BankStatementLine).where(*where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    sort_col = _SORT_COLUMNS.get(sort, BankStatementLine.txn_date)
    primary = sort_col.asc() if direction == "asc" else sort_col.desc()
    # created_at tie-breaker keeps the order stable when the primary
    # column has duplicates (e.g. same txn_date).
    tiebreak = (
        BankStatementLine.created_at.asc() if direction == "asc"
        else BankStatementLine.created_at.desc()
    )

    stmt = (
        select(BankStatementLine)
        .where(*where)
        .order_by(primary, tiebreak)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def api_get(
    session: AsyncSession,
    line_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> BankStatementLine | None:
    """Fetch a single bank statement line. Returns None if not found.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    """
    if tenant_id is None:
        return await session.get(BankStatementLine, line_id)
    result = await session.execute(
        select(BankStatementLine).where(
            BankStatementLine.id == line_id,
            BankStatementLine.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    account_id: uuid.UUID,
    txn_date: date,
    amount: Decimal,
    description: str | None = None,
    balance: Decimal | None = None,
    reference: str | None = None,
    status: StatementLineStatus = StatementLineStatus.UNMATCHED,
    external_id: str | None = None,
    bank_feed_account_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
) -> BankStatementLine:
    """Create a new bank statement line with change_log entry."""
    line = BankStatementLine(
        company_id=company_id,
        tenant_id=tenant_id,
        account_id=account_id,
        txn_date=txn_date,
        amount=amount,
        description=description,
        balance=balance,
        reference=reference,
        status=status,
        external_id=external_id,
        bank_feed_account_id=bank_feed_account_id,
        contact_id=contact_id,
        version=1,
    )
    session.add(line)
    await session.flush()
    await session.refresh(line)

    await change_log_svc.append(
        session,
        entity="bank_statement_line",
        entity_id=line.id,
        op="created",
        actor=actor,
        payload=_serialise(line),
        version=line.version,
    )
    await session.commit()
    await session.refresh(line)
    return line


async def api_update(
    session: AsyncSession,
    line_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    description: str | None = None,
    reference: str | None = None,
    status: StatementLineStatus | None = None,
    matched_entry_id: uuid.UUID | None = None,
    matched_at: datetime | None = None,
    matched_by: str | None = None,
    contact_id: uuid.UUID | None = None,
    balance: Decimal | None = None,
) -> BankStatementLine:
    """Update a bank statement line with optimistic locking + change_log."""
    line = await session.get(BankStatementLine, line_id)
    if line is None or line.archived_at is not None:
        raise BankStatementLineError(f"BankStatementLine {line_id} not found")
    if line.version != expected_version:
        raise VersionConflict(line)

    if description is not None:
        line.description = description
    if reference is not None:
        line.reference = reference
    if status is not None:
        line.status = status
    if matched_entry_id is not None:
        line.matched_entry_id = matched_entry_id
    if matched_at is not None:
        line.matched_at = matched_at
    if matched_by is not None:
        line.matched_by = matched_by
    if contact_id is not None:
        line.contact_id = contact_id
    if balance is not None:
        line.balance = balance

    line.version = line.version + 1
    await session.flush()
    await session.refresh(line)

    await change_log_svc.append(
        session,
        entity="bank_statement_line",
        entity_id=line.id,
        op="updated",
        actor=actor,
        payload=_serialise(line),
        version=line.version,
    )
    await session.commit()
    await session.refresh(line)
    return line


async def api_delete(
    session: AsyncSession,
    line_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> BankStatementLine:
    """Soft-archive a bank statement line with optimistic locking + change_log."""
    line = await session.get(BankStatementLine, line_id)
    if line is None or line.archived_at is not None:
        raise BankStatementLineError(f"BankStatementLine {line_id} not found")
    if line.version != expected_version:
        raise VersionConflict(line)

    line.archived_at = datetime.now(UTC)
    line.version = line.version + 1
    await session.flush()
    await session.refresh(line)

    await change_log_svc.append(
        session,
        entity="bank_statement_line",
        entity_id=line.id,
        op="deleted",
        actor=actor,
        payload=_serialise(line),
        version=line.version,
    )
    await session.commit()
    return line


_VALID_MATCHED_TO_TYPES = {"PAYMENT", "JOURNAL_ENTRY"}


async def api_match(
    session: AsyncSession,
    line_id: uuid.UUID,
    actor: str,
    matched_to_type: str,
    matched_to_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> BankStatementLine:
    """Set a bank statement line to MATCHED, recording what it was matched to.

    matched_to_type must be 'PAYMENT' or 'JOURNAL_ENTRY'.
    matched_to_id is the UUID of the matching record (not FK-constrained).
    """
    if matched_to_type not in _VALID_MATCHED_TO_TYPES:
        valid = ", ".join(sorted(_VALID_MATCHED_TO_TYPES))
        raise BankStatementLineError(
            f"matched_to_type must be one of {valid}, got '{matched_to_type}'"
        )

    line = await session.get(BankStatementLine, line_id)
    if line is None or line.archived_at is not None:
        raise BankStatementLineError(f"BankStatementLine {line_id} not found")

    line.status = StatementLineStatus.MATCHED
    line.matched_to_type = matched_to_type
    line.matched_to_id = matched_to_id
    line.matched_at = datetime.now(UTC)
    line.matched_by = actor
    line.version = line.version + 1

    await session.flush()
    await session.refresh(line)

    await change_log_svc.append(
        session,
        entity="bank_statement_line",
        entity_id=line.id,
        op="matched",
        actor=actor,
        payload=_serialise(line),
        version=line.version,
    )
    await session.commit()
    await session.refresh(line)
    return line


async def api_unmatch(
    session: AsyncSession,
    line_id: uuid.UUID,
    actor: str,
    tenant_id: uuid.UUID,
) -> BankStatementLine:
    """Clear all match fields and set status back to UNMATCHED."""
    line = await session.get(BankStatementLine, line_id)
    if line is None or line.archived_at is not None:
        raise BankStatementLineError(f"BankStatementLine {line_id} not found")

    line.status = StatementLineStatus.UNMATCHED
    line.matched_to_type = None
    line.matched_to_id = None
    line.matched_entry_id = None
    line.matched_at = None
    line.matched_by = None
    line.version = line.version + 1

    await session.flush()
    await session.refresh(line)

    await change_log_svc.append(
        session,
        entity="bank_statement_line",
        entity_id=line.id,
        op="unmatched",
        actor=actor,
        payload=_serialise(line),
        version=line.version,
    )
    await session.commit()
    await session.refresh(line)
    return line


__all__ = [
    "BankStatementLineError",
    "VersionConflict",
    "api_create",
    "api_delete",
    "api_get",
    "api_match",
    "api_unmatch",
    "api_update",
    "list_active",
]
