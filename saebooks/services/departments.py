"""Department and CostCentre services — CRUD + list with tenant_id defence.

Both models are thin dimension tables (code + name) per company.
These services provide list / get / create / archive for the API
and Jinja layers; the previous access pattern was ad-hoc inline
select() calls in reports.py (labelling only).

P0 defence-in-depth: every list and get function accepts an optional
``tenant_id`` parameter.  When supplied the query is additionally
filtered by tenant_id, so a corrupted row where company_id and
tenant_id disagree cannot leak to the wrong tenant.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.department import CostCentre, Department

# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------


async def list_departments(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> list[Department]:
    """List departments for a company, optionally scoped by tenant.

    P0 defence-in-depth: when ``tenant_id`` is supplied the result is
    additionally filtered so a corrupt row cannot appear in the wrong
    tenant's list.
    """
    stmt = select(Department).where(Department.company_id == company_id)
    if tenant_id is not None:
        stmt = stmt.where(Department.tenant_id == tenant_id)
    if not include_archived:
        stmt = stmt.where(Department.archived_at.is_(None))
    stmt = stmt.order_by(Department.code)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_department(
    session: AsyncSession,
    dept_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Department | None:
    """Fetch a department by id.

    When ``tenant_id`` is supplied, a foreign-tenant id returns ``None``
    even if the row exists.
    """
    if tenant_id is None:
        return await session.get(Department, dept_id)
    result = await session.execute(
        select(Department).where(
            Department.id == dept_id,
            Department.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


async def create_department(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    code: str,
    name: str,
) -> Department:
    """Create a new department row."""
    dept = Department(
        company_id=company_id,
        tenant_id=tenant_id,
        code=code.strip(),
        name=name.strip(),
        version=1,
    )
    session.add(dept)
    await session.commit()
    await session.refresh(dept)
    return dept


async def archive_department(
    session: AsyncSession,
    dept_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Department | None:
    """Soft-archive a department.

    When ``tenant_id`` is supplied, a foreign-tenant id is silently
    treated as not-found.
    """
    dept = await get_department(session, dept_id, tenant_id=tenant_id)
    if dept is None:
        return None
    dept.archived_at = datetime.now(UTC)
    await session.commit()
    return dept


# ---------------------------------------------------------------------------
# Cost centres
# ---------------------------------------------------------------------------


async def list_cost_centres(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> list[CostCentre]:
    """List cost centres for a company, optionally scoped by tenant.

    P0 defence-in-depth: when ``tenant_id`` is supplied the result is
    additionally filtered so a corrupt row cannot appear in the wrong
    tenant's list.
    """
    stmt = select(CostCentre).where(CostCentre.company_id == company_id)
    if tenant_id is not None:
        stmt = stmt.where(CostCentre.tenant_id == tenant_id)
    if not include_archived:
        stmt = stmt.where(CostCentre.archived_at.is_(None))
    stmt = stmt.order_by(CostCentre.code)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_cost_centre(
    session: AsyncSession,
    cc_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> CostCentre | None:
    """Fetch a cost centre by id.

    When ``tenant_id`` is supplied, a foreign-tenant id returns ``None``
    even if the row exists.
    """
    if tenant_id is None:
        return await session.get(CostCentre, cc_id)
    result = await session.execute(
        select(CostCentre).where(
            CostCentre.id == cc_id,
            CostCentre.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


async def create_cost_centre(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    code: str,
    name: str,
) -> CostCentre:
    """Create a new cost centre row."""
    cc = CostCentre(
        company_id=company_id,
        tenant_id=tenant_id,
        code=code.strip(),
        name=name.strip(),
        version=1,
    )
    session.add(cc)
    await session.commit()
    await session.refresh(cc)
    return cc


async def archive_cost_centre(
    session: AsyncSession,
    cc_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> CostCentre | None:
    """Soft-archive a cost centre.

    When ``tenant_id`` is supplied, a foreign-tenant id is silently
    treated as not-found.
    """
    cc = await get_cost_centre(session, cc_id, tenant_id=tenant_id)
    if cc is None:
        return None
    cc.archived_at = datetime.now(UTC)
    await session.commit()
    return cc
