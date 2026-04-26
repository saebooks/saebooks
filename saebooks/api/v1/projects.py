"""JSON router — ``/api/v1/projects``.

Phase 1 tier-4 projects endpoint.

Projects are flat job/cost-centre tags attached to transaction lines
for job costing and project-level P&L reporting.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    ProjectConflictBody,
    ProjectCreate,
    ProjectListOut,
    ProjectOut,
    ProjectUpdate,
)
from saebooks.models.company import Company
from saebooks.services import projects as svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession, tenant_id: UUID) -> UUID:
    """Return the first active company for the request tenant."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(project: Any) -> dict[str, Any]:
    return json.loads(ProjectOut.model_validate(project).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ProjectListOut)
async def list_projects(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> ProjectListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    items, total = await svc.list_projects(
        session,
        company_id,
        tenant_id,
        status=status,
        archived=archived,
        limit=page_size,
        offset=offset,
    )
    return ProjectListOut(
        items=[ProjectOut.model_validate(p) for p in items],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    request: Request,
    project_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    tenant_id = resolve_tenant_id(request)
    project = await svc.api_get(session, project_id, tenant_id=tenant_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return ProjectOut.model_validate(project)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    request: Request,
    payload: ProjectCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    company_id = await _first_company_id(session, tenant_id)
    try:
        project = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            code=payload.code,
            name=payload.name,
            status=payload.status,
            start_date=payload.start_date,
            end_date=payload.end_date,
            notes=payload.notes,
            extra=payload.extra,
        )
    except (ValueError, svc.ProjectApiError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(project)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{project_id}",
    responses={
        200: {"model": ProjectOut},
        409: {"model": ProjectConflictBody, "description": "Version mismatch"},
    },
)
async def update_project(
    request: Request,
    project_id: UUID,
    payload: ProjectUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with project version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify project belongs to this tenant
    if await svc.api_get(session, project_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Project not found")

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    try:
        project = await svc.api_update(
            session,
            project_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        body = ProjectConflictBody(
            detail="version mismatch",
            current=ProjectOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ProjectApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(project)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{project_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": ProjectConflictBody, "description": "Version mismatch"},
    },
)
async def delete_project(
    request: Request,
    project_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with project version is required")

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify project belongs to this tenant
    if await svc.api_get(session, project_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Project not found")

    try:
        await svc.api_delete(
            session,
            project_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = ProjectConflictBody(
            detail="version mismatch",
            current=ProjectOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ProjectApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)
