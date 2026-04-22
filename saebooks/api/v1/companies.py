"""Pure JSON companies router — ``/api/v1/companies``.

Phase 1 tier-1 entity. FLAG_MULTI_COMPANY exists in the codebase.

Endpoints:
  GET  /api/v1/companies          — list all active companies
  GET  /api/v1/companies/{id}     — get one company
  PATCH /api/v1/companies/{id}    — update metadata with If-Match

Create and archive are intentionally omitted from the JSON API at
Phase 1: creating companies requires licence-cap enforcement via the
portal JWT (Phase 2+), and archiving a company is a destructive
multi-step operation that needs explicit UX flow. The Jinja UI still
handles those paths via the service layer.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.schemas import (
    CompanyConflictBody,
    CompanyListOut,
    CompanyOut,
    CompanyUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import companies as svc
from sqlalchemy import func, select

router = APIRouter(
    prefix="/companies",
    tags=["companies"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(session: Any, key: UUID) -> JSONResponse | None:
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(content=existing.response_body, status_code=existing.response_status)


async def _remember_idempotent(
    session: Any, key: UUID, body: dict[str, Any], status_code: int
) -> None:
    row = IdempotencyKey(key=key, response_body=body, response_status=status_code)
    session.add(row)
    await session.flush()


def _dump(company: Company) -> dict[str, Any]:
    return json.loads(CompanyOut.model_validate(company).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CompanyListOut)
async def list_companies(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CompanyListOut:
    async with AsyncSessionLocal() as session:
        total_stmt = (
            select(func.count())
            .select_from(Company)
            .where(Company.archived_at.is_(None))
        )
        total = (await session.execute(total_stmt)).scalar_one()
        stmt = (
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.name)
            .offset(offset)
            .limit(limit)
        )
        companies = list((await session.execute(stmt)).scalars().all())
        return CompanyListOut(
            items=[CompanyOut.model_validate(c) for c in companies],
            total=total,
        )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(company_id: UUID) -> CompanyOut:
    async with AsyncSessionLocal() as session:
        company = await svc.get(session, company_id)
        if company is None or company.archived_at is not None:
            raise HTTPException(404, "Company not found")
        return CompanyOut.model_validate(company)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{company_id}",
    responses={
        200: {"model": CompanyOut},
        409: {"model": CompanyConflictBody, "description": "Version mismatch"},
    },
)
async def update_company(
    company_id: UUID,
    payload: CompanyUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with company version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        try:
            company = await svc.update(
                session,
                company_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                **payload.model_dump(exclude_unset=True),
            )
        except svc.VersionConflict as exc:
            await session.refresh(exc.current)
            body = CompanyConflictBody(
                detail="version mismatch",
                current=CompanyOut.model_validate(exc.current),
            ).model_dump(mode="json")
            if key is not None:
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except ValueError as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        await session.refresh(company)
        body = _dump(company)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
            await session.commit()
    return JSONResponse(body, status_code=200)
