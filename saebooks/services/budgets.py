"""Budget service — monthly budget amounts per (company, account, month).

Budgets are a pure reporting overlay — they never hit the GL. The
primary write path is :func:`upsert` (single row) or :func:`bulk_upsert`
(a whole year for one account) invoked by the grid-edit UI.

Reads: :func:`list_for_period` returns a raw list; the budget-vs-actual
report in :mod:`saebooks.services.reports` rolls those up against
actuals.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.budget import Budget


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
