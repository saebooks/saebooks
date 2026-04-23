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

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    ProjectConflictBody,
    ProjectCreate,
    ProjectListOut,
    ProjectOut,
    ProjectUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import projects as svc

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session) -> UUID:
    """Return the first active company — Phase 1 single-company assumption."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
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


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(session, key: UUID) -> JSONResponse | None:
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(content=existing.response_body, status_code=existing.response_status)


async def _remember_idempotent(
    session, key: UUID, body: dict[str, Any], status_code: int
) -> None:
    row = IdempotencyKey(key=key, response_body=body, response_status=status_code)
    session.add(row)
    await session.flush()


def _dump(project: Any) -> dict[str, Any]:
    return json.loads(ProjectOut.model_validate(project).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ProjectListOut)
async def list_projects(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    archived: bool = Query(default=False),
) -> ProjectListOut:
    offset = (page - 1) * page_size
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
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
async def get_project(project_id: UUID) -> ProjectOut:
    async with AsyncSessionLocal() as session:
        project = await svc.api_get(session, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        return ProjectOut.model_validate(project)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
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
            await _remember_idempotent(session, key, body, 201)
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
    project_id: UUID,
    payload: ProjectUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with project version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

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
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.ProjectApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(project)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
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
    project_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with project version is required")

    async with AsyncSessionLocal() as session:
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
