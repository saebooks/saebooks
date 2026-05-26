"""Service layer for one-off vendors.

One-off vendors are lightweight party records for COD / walk-in / single-purchase
suppliers that live outside the contacts table.  The DB table has FORCE ROW LEVEL
SECURITY with a ``tenant_isolation`` policy, but all functions also filter on
``tenant_id`` explicitly (defence-in-depth, matching the contacts service pattern).

Read-only columns (managed by DB triggers / usage tracking):
  ``last_used_at``, ``use_count``, ``total_spent``, ``promoted_to_contact_id``
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.one_off_vendor import OneOffVendor

# ABN: exactly 11 digits after stripping spaces
_ABN_RE = re.compile(r"^\d{11}$")

_WRITABLE_FIELDS = {"name", "abn", "default_account_id", "default_tax_code", "notes"}


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value."""

    def __init__(self, current: OneOffVendor) -> None:
        super().__init__(
            f"OneOffVendor {current.id} is at version {current.version}, not the expected version"
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
) -> tuple[list[OneOffVendor], int]:
    """Return (items, total) for active one-off vendors, filtered by company + tenant."""
    base = (
        select(OneOffVendor)
        .where(
            OneOffVendor.company_id == company_id,
            OneOffVendor.tenant_id == tenant_id,
            OneOffVendor.archived_at.is_(None),
        )
    )
    if search:
        base = base.where(OneOffVendor.name.ilike(f"%{search}%"))

    count_stmt = select(func.count()).select_from(base.subquery())
    total: int = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        base.order_by(OneOffVendor.name)
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(items_stmt)).scalars().all())
    return rows, total


async def get(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
) -> OneOffVendor | None:
    """Fetch a one-off vendor by id, scoped to tenant (and optionally company)."""
    clauses = [
        OneOffVendor.id == vendor_id,
        OneOffVendor.tenant_id == tenant_id,
    ]
    if company_id is not None:
        clauses.append(OneOffVendor.company_id == company_id)
    result = await session.execute(select(OneOffVendor).where(*clauses))
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
) -> OneOffVendor:
    """Create a new one-off vendor and commit."""
    if abn is not None:
        abn = _validate_abn(abn)

    vendor = OneOffVendor(
        company_id=company_id,
        tenant_id=tenant_id,
        name=name.strip(),
        abn=abn,
        default_account_id=default_account_id,
        default_tax_code=default_tax_code,
        notes=notes,
        version=1,
    )
    session.add(vendor)
    await session.flush()
    await session.refresh(vendor)
    await session.commit()
    return vendor


async def update(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
    actor: str = "api",
    expected_version: int | None = None,
    **kwargs: Any,
) -> OneOffVendor:
    """Update writable fields on a one-off vendor."""
    vendor = await get(session, vendor_id, tenant_id=tenant_id, company_id=company_id)
    if vendor is None:
        raise ValueError(f"OneOffVendor {vendor_id} not found")

    if expected_version is not None and vendor.version != expected_version:
        raise VersionConflict(vendor)

    if "abn" in kwargs and kwargs["abn"] is not None:
        kwargs["abn"] = _validate_abn(kwargs["abn"])
    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    for key, value in kwargs.items():
        if key not in _WRITABLE_FIELDS:
            raise ValueError(f"Unknown or read-only field: {key}")
        setattr(vendor, key, value)

    vendor.version = vendor.version + 1
    await session.flush()
    await session.refresh(vendor)
    await session.commit()
    return vendor


async def archive(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
    actor: str = "api",
    expected_version: int | None = None,
) -> OneOffVendor | None:
    """Soft-delete: set archived_at. Returns None if not found."""
    vendor = await get(session, vendor_id, tenant_id=tenant_id, company_id=company_id)
    if vendor is None:
        return None
    if expected_version is not None and vendor.version != expected_version:
        raise VersionConflict(vendor)
    vendor.archived_at = datetime.now(UTC)
    vendor.version = vendor.version + 1
    await session.flush()
    await session.refresh(vendor)
    await session.commit()
    return vendor
