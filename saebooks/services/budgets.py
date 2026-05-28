"""Budget service — monthly budget amounts per (company, account, month).

Budgets are a pure reporting overlay — they never hit the GL. The
primary write path is :func:`upsert` (single row) or :func:`bulk_upsert`
(a whole year for one account) invoked by the grid-edit UI.

Reads: :func:`list_for_period` returns a raw list; the budget-vs-actual
report in :mod:`saebooks.services.reports` rolls those up against
actuals.

API-tier functions (``api_*``) added for ``/api/v1/budgets``. They live
alongside the legacy functions and must not break them.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func as sa_func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.budget import Budget
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Columns serialised into change_log.payload.
_BUDGET_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "account_id",
    "year",
    "month",
    "amount",
    "notes",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetApiError(ValueError):
    """Raised on validation or state-transition failure (API tier)."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Budget) -> None:
        super().__init__(
            f"Budget {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise(b: Budget) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _BUDGET_COLUMNS:
        val = getattr(b, key, None)
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


async def get(session: AsyncSession, budget_id: uuid.UUID) -> Budget | None:
    return await session.get(Budget, budget_id)


async def upsert(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    year: int,
    month: int,
    amount: Decimal,
    notes: str | None = None,
) -> Budget:
    """Insert-or-update one row keyed on (company, account, year, month).

    Uses Postgres `ON CONFLICT DO UPDATE` so double-submits + grid
    edits are idempotent. Returns the persisted row (post-commit).
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month {month} is out of range 1..12")

    stmt = (
        pg_insert(Budget)
        .values(
            company_id=company_id,
            account_id=account_id,
            year=year,
            month=month,
            amount=amount,
            notes=notes,
        )
        .on_conflict_do_update(
            constraint="uq_budgets_company_account_year_month",
            set_={"amount": amount, "notes": notes},
        )
        .returning(Budget)
    )
    result = await session.execute(stmt)
    await session.commit()
    row = result.scalar_one()
    await session.refresh(row)
    return row


async def list_for_period(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    year: int,
    month_from: int = 1,
    month_to: int = 12,
    account_id: uuid.UUID | None = None,
) -> list[Budget]:
    """Return budget rows inside an inclusive month window.

    Default is a whole year. ``account_id`` filters to one account for
    the grid-edit UI.
    """
    stmt = (
        select(Budget)
        .where(
            Budget.company_id == company_id,
            Budget.year == year,
            Budget.month >= month_from,
            Budget.month <= month_to,
        )
    )
    if account_id is not None:
        stmt = stmt.where(Budget.account_id == account_id)
    stmt = stmt.order_by(Budget.account_id, Budget.month)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def bulk_upsert(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    year: int,
    rows: list[dict[str, Any]],
) -> int:
    """Apply a full-year grid of budget rows in one transaction.

    Each row is ``{"account_id": UUID, "month": int, "amount": Decimal,
    "notes": str | None}``. Zero-amount rows are deleted (keeps the
    table tidy and makes the "unset" UX cheap).

    Returns the count of rows written (upserts + deletes).
    """
    if not rows:
        return 0

    written = 0
    for row in rows:
        month = int(row["month"])
        if not 1 <= month <= 12:
            raise ValueError(f"month {month} is out of range 1..12")
        amount = Decimal(str(row.get("amount", 0)))
        if amount == Decimal("0"):
            # Purge the row rather than carry a meaningless zero.
            await session.execute(
                delete(Budget).where(
                    Budget.company_id == company_id,
                    Budget.account_id == row["account_id"],
                    Budget.year == year,
                    Budget.month == month,
                )
            )
            written += 1
            continue
        stmt = (
            pg_insert(Budget)
            .values(
                company_id=company_id,
                account_id=row["account_id"],
                year=year,
                month=month,
                amount=amount,
                notes=row.get("notes"),
            )
            .on_conflict_do_update(
                constraint="uq_budgets_company_account_year_month",
                set_={"amount": amount, "notes": row.get("notes")},
            )
        )
        await session.execute(stmt)
        written += 1

    await session.commit()
    return written


async def delete_budget(
    session: AsyncSession,
    budget_id: uuid.UUID,
) -> None:
    """Hard-delete a budget row. Budgets have no FK out to anything
    business-critical, so hard-delete is safe here."""
    row = await session.get(Budget, budget_id)
    if row is None:
        return
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# API-tier CRUD  (added for /api/v1/budgets — cycle 16)
# ---------------------------------------------------------------------------


async def api_get(
    session: AsyncSession,
    budget_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Budget | None:
    """Fetch a single budget row by primary key.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    """
    if tenant_id is None and company_id is None:
        return await session.get(Budget, budget_id)
    clauses = [Budget.id == budget_id]
    if tenant_id is not None:
        clauses.append(Budget.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Budget.company_id == company_id)
    result = await session.execute(
        select(Budget).where(*clauses)
    )
    return result.scalars().first()


async def list_budgets(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    year: int | None = None,
    month: int | None = None,
    account_id: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Budget], int]:
    """Return (budget_rows, total_count) filtered by year/month/account/archived."""
    where = [Budget.company_id == company_id]

    if not archived:
        where.append(Budget.archived_at.is_(None))
    else:
        where.append(Budget.archived_at.isnot(None))

    if year is not None:
        where.append(Budget.year == year)

    if month is not None:
        where.append(Budget.month == month)

    if account_id is not None:
        where.append(Budget.account_id == uuid.UUID(account_id))

    count_stmt = (
        select(sa_func.count())
        .select_from(Budget)
        .where(*where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Budget)
        .where(*where)
        .order_by(Budget.year, Budget.month, Budget.account_id)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    account_id: uuid.UUID,
    year: int,
    month: int,
    amount: Decimal,
    notes: str | None = None,
) -> Budget:
    """Create a budget row with version=1 and change_log entry."""
    if not 1 <= month <= 12:
        raise BudgetApiError(f"month {month} is out of range 1..12")

    b = Budget(
        company_id=company_id,
        tenant_id=tenant_id,
        account_id=account_id,
        year=year,
        month=month,
        amount=amount,
        notes=notes,
        version=1,
    )
    session.add(b)
    await session.flush()
    await session.refresh(b)

    await change_log_svc.append(
        session,
        entity="budget",
        entity_id=b.id,
        op="created",
        actor=actor,
        payload=_serialise(b),
        version=b.version,
    )
    await session.commit()
    result = await session.get(Budget, b.id)
    assert result is not None
    return result


async def api_update(
    session: AsyncSession,
    budget_id: uuid.UUID,
    actor: str,
    expected_version: int,
    **kwargs: Any,
) -> Budget:
    """Update a budget row with optimistic locking + change_log."""
    b = await session.get(Budget, budget_id)
    if b is None:
        raise BudgetApiError(f"Budget {budget_id} not found")
    if b.version != expected_version:
        raise VersionConflict(b)

    _ALLOWED_FIELDS = frozenset({"account_id", "year", "month", "amount", "notes"})

    for key, value in kwargs.items():
        if key not in _ALLOWED_FIELDS:
            raise BudgetApiError(f"Unknown or non-editable field: {key}")
        if key == "month" and value is not None:
            if not 1 <= int(value) <= 12:
                raise BudgetApiError(f"month {value} is out of range 1..12")
        setattr(b, key, value)

    b.version = b.version + 1
    await session.flush()
    await session.refresh(b)

    await change_log_svc.append(
        session,
        entity="budget",
        entity_id=b.id,
        op="updated",
        actor=actor,
        payload=_serialise(b),
        version=b.version,
    )
    await session.commit()
    result = await session.get(Budget, budget_id)
    assert result is not None
    return result


async def api_delete(
    session: AsyncSession,
    budget_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Budget:
    """Soft-archive a budget row with optimistic locking + change_log."""
    b = await session.get(Budget, budget_id)
    if b is None:
        raise BudgetApiError(f"Budget {budget_id} not found")
    if b.version != expected_version:
        raise VersionConflict(b)

    b.archived_at = datetime.now(UTC)
    b.version = b.version + 1
    await session.flush()
    await session.refresh(b)

    await change_log_svc.append(
        session,
        entity="budget",
        entity_id=b.id,
        op="deleted",
        actor=actor,
        payload=_serialise(b),
        version=b.version,
    )
    await session.commit()
    return b


__all__ = [
    "BudgetApiError",
    "VersionConflict",
    "api_create",
    "api_delete",
    "api_get",
    "api_update",
    "bulk_upsert",
    "delete_budget",
    "get",
    "list_budgets",
    "list_for_period",
    "upsert",
]
