"""Service layer for one-off customers.

One-off customers are lightweight party records for single-invoice / walk-in customers
that live outside the contacts table.  Symmetric to the one_off_vendors service.

Read-only columns (managed by DB triggers / usage tracking):
  ``last_used_at``, ``use_count``, ``total_billed``, ``promoted_to_contact_id``
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.one_off_customer import OneOffCustomer

_ABN_RE = re.compile(r"^\d{11}$")

_WRITABLE_FIELDS = {"name", "abn", "default_account_id", "default_tax_code", "notes"}


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value."""

    def __init__(self, current: OneOffCustomer) -> None:
        super().__init__(
            f"OneOffCustomer {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


def _validate_abn(raw: str) -> str:
    cleaned = raw.replace(" ", "")
    if not _ABN_RE.match(cleaned):
        raise ValueError(
            f"Invalid ABN '{raw}' — must be exactly 11 digits (spaces are allowed)."
        )
    return cleaned


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[OneOffCustomer], int]:
    """Return (items, total) for active one-off customers, filtered by company + tenant."""
    base = (
        select(OneOffCustomer)
        .where(
            OneOffCustomer.company_id == company_id,
            OneOffCustomer.tenant_id == tenant_id,
            OneOffCustomer.archived_at.is_(None),
        )
    )
    if search:
        base = base.where(OneOffCustomer.name.ilike(f"%{search}%"))

    count_stmt = select(func.count()).select_from(base.subquery())
    total: int = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        base.order_by(OneOffCustomer.name)
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(items_stmt)).scalars().all())
    return rows, total


async def get(
    session: AsyncSession,
    customer_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
) -> OneOffCustomer | None:
    """Fetch a one-off customer by id, scoped to tenant (and optionally company)."""
    clauses = [
        OneOffCustomer.id == customer_id,
        OneOffCustomer.tenant_id == tenant_id,
    ]
    if company_id is not None:
        clauses.append(OneOffCustomer.company_id == company_id)
    result = await session.execute(select(OneOffCustomer).where(*clauses))
    return result.scalars().first()


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor: str = "api",
    name: str,
    abn: str | None = None,
    default_account_id: uuid.UUID | None = None,
    default_tax_code: str | None = None,
    notes: str | None = None,
) -> OneOffCustomer:
    """Create a new one-off customer and commit."""
    if abn is not None:
        abn = _validate_abn(abn)

    customer = OneOffCustomer(
        company_id=company_id,
        tenant_id=tenant_id,
        name=name.strip(),
        abn=abn,
        default_account_id=default_account_id,
        default_tax_code=default_tax_code,
        notes=notes,
        version=1,
    )
    session.add(customer)
    await session.flush()
    await session.refresh(customer)
    await session.commit()
    return customer


async def update(
    session: AsyncSession,
    customer_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
    actor: str = "api",
    expected_version: int | None = None,
    **kwargs: Any,
) -> OneOffCustomer:
    """Update writable fields on a one-off customer."""
    customer = await get(session, customer_id, tenant_id=tenant_id, company_id=company_id)
    if customer is None:
        raise ValueError(f"OneOffCustomer {customer_id} not found")

    if expected_version is not None and customer.version != expected_version:
        raise VersionConflict(customer)

    if "abn" in kwargs and kwargs["abn"] is not None:
        kwargs["abn"] = _validate_abn(kwargs["abn"])
    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    for key, value in kwargs.items():
        if key not in _WRITABLE_FIELDS:
            raise ValueError(f"Unknown or read-only field: {key}")
        setattr(customer, key, value)

    customer.version = customer.version + 1
    await session.flush()
    await session.refresh(customer)
    await session.commit()
    return customer


async def archive(
    session: AsyncSession,
    customer_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
    actor: str = "api",
    expected_version: int | None = None,
) -> OneOffCustomer | None:
    """Soft-delete: set archived_at. Returns None if not found."""
    customer = await get(session, customer_id, tenant_id=tenant_id, company_id=company_id)
    if customer is None:
        return None
    if expected_version is not None and customer.version != expected_version:
        raise VersionConflict(customer)
    customer.archived_at = datetime.now(UTC)
    customer.version = customer.version + 1
    await session.flush()
    await session.refresh(customer)
    await session.commit()
    return customer
