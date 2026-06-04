"""JSON router — ``/api/v1/branches``.

CRUD for company-scoped Branches (migration 0134). Branches are internal
sub-divisional tags on transactions; not a legal entity. See
``saebooks/models/branch.py`` for the schema rationale.

* Bearer-token auth via ``require_bearer``.
* All operations are company-scoped via ``get_active_company_id`` (so
  the X-Company-Id header / fallback resolution picks the right company).
* PATCH/DELETE work by ID; code is immutable, used for the unique
  ``(company_id, code)`` constraint.
* DELETE is a soft-archive (sets archived_at).
* ``is_default=true`` is enforced unique-per-company by a partial unique
  index — flipping a different branch to default un-defaults the old
  one inside the same transaction.
"""
from __future__ import annotations

import json
from datetime import UTC
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    BranchCreate,
    BranchListOut,
    BranchOut,
    BranchUpdate,
)
from saebooks.models.branch import Branch

router = APIRouter(
    prefix="/branches",
    tags=["branches"],
    dependencies=[Depends(require_bearer)],
)


def _dump(branch: Branch) -> dict[str, Any]:
    return json.loads(BranchOut.model_validate(branch).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BranchListOut)
async def list_branches(
    request: Request,
    include_archived: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BranchListOut:
    where = [Branch.company_id == company_id]
    if not include_archived:
        where.append(Branch.archived_at.is_(None))

    count_stmt = select(func.count()).select_from(Branch).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Branch)
        .where(*where)
        .order_by(Branch.is_default.desc(), Branch.code)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return BranchListOut(items=items, total=total)


@router.get("/{branch_id}", response_model=BranchOut)
async def get_branch(
    branch_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BranchOut:
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id)
        )
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(404, "Branch not found")
    return BranchOut.model_validate(branch)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_branch(
    request: Request,
    payload: BranchCreate,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)

    # If is_default=true, un-default any current default for this company
    if payload.is_default:
        await session.execute(
            select(Branch).where(
                Branch.company_id == company_id,
                Branch.is_default.is_(True),
                Branch.archived_at.is_(None),
            )
        )
        # Use update statement instead of fetch-and-set so we don't load
        # all existing rows.
        from sqlalchemy import update
        await session.execute(
            update(Branch)
            .where(
                Branch.company_id == company_id,
                Branch.is_default.is_(True),
                Branch.archived_at.is_(None),
            )
            .values(is_default=False, version=Branch.version + 1)
        )

    branch = Branch(
        company_id=company_id,
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        is_default=payload.is_default,
    )
    session.add(branch)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409,
            f"Branch with code '{payload.code}' already exists for this company",
        ) from exc
    await session.refresh(branch)
    return JSONResponse(_dump(branch), status_code=201)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch("/{branch_id}", response_model=BranchOut)
async def update_branch(
    branch_id: UUID,
    payload: BranchUpdate,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BranchOut:
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id)
        )
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(404, "Branch not found")

    # If turning this one INTO the default, clear any other default first.
    if payload.is_default is True and not branch.is_default:
        from sqlalchemy import update
        await session.execute(
            update(Branch)
            .where(
                Branch.company_id == company_id,
                Branch.id != branch_id,
                Branch.is_default.is_(True),
                Branch.archived_at.is_(None),
            )
            .values(is_default=False, version=Branch.version + 1)
        )

    if payload.name is not None:
        branch.name = payload.name
    if payload.is_default is not None:
        branch.is_default = payload.is_default
    branch.version += 1
    await session.commit()
    await session.refresh(branch)
    return BranchOut.model_validate(branch)


# ---------------------------------------------------------------------------
# Archive (soft delete)
# ---------------------------------------------------------------------------


@router.delete("/{branch_id}", status_code=204)
async def archive_branch(
    branch_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id)
        )
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(404, "Branch not found")
    if branch.archived_at is not None:
        return Response(status_code=204)
    if branch.is_default:
        raise HTTPException(
            409, "Cannot archive the default branch. Mark another branch default first."
        )
    from datetime import datetime
    branch.archived_at = datetime.now(tz=UTC)
    branch.version += 1
    await session.commit()
    return Response(status_code=204)
