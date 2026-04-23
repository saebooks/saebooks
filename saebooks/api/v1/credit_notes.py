"""JSON router — ``/api/v1/credit_notes``.

Phase 1 tier-3 credit notes endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    CreditNoteConflictBody,
    CreditNoteCreate,
    CreditNoteListOut,
    CreditNoteOut,
    CreditNoteUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.credit_note import CreditNoteStatus
from saebooks.services import credit_notes as svc

router = APIRouter(
    prefix="/credit_notes",
    tags=["credit_notes"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession) -> UUID:
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


def _dump(cn: Any) -> dict[str, Any]:
    return json.loads(CreditNoteOut.model_validate(cn).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CreditNoteListOut)
async def list_credit_notes(
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> CreditNoteListOut:
    offset = (page - 1) * page_size
    status_enum: CreditNoteStatus | None = None
    if status is not None:
        try:
            status_enum = CreditNoteStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        notes, total = await svc.list_active(
            session,
            company_id,
            tenant_id,
            contact_id=contact_id,
            status=status_enum,
            date_from=date_from,
            date_to=date_to,
            limit=page_size,
            offset=offset,
        )
        return CreditNoteListOut(
            items=[CreditNoteOut.model_validate(cn) for cn in notes],
            total=total,
            limit=page_size,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{credit_note_id}", response_model=CreditNoteOut)
async def get_credit_note(credit_note_id: UUID) -> CreditNoteOut:
    async with AsyncSessionLocal() as session:
        cn = await svc.api_get(session, credit_note_id)
        if cn is None:
            raise HTTPException(404, "Credit note not found")
        return CreditNoteOut.model_validate(cn)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=CreditNoteOut, status_code=201)
async def create_credit_note(
    payload: CreditNoteCreate,
    bearer: str = Depends(require_bearer),
) -> Any:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        try:
            cn = await svc.api_create(
                session,
                company_id,
                tenant_id,
                actor=f"api:{bearer[:8]}…",
                contact_id=payload.contact_id,
                issue_date=payload.issue_date,
                lines=[line.model_dump() for line in payload.lines],
                original_invoice_id=payload.original_invoice_id,
                reason=payload.reason,
                notes=payload.notes,
            )
        except (ValueError, svc.CreditNoteError) as exc:
            raise HTTPException(422, str(exc)) from exc

        body = _dump(cn)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{credit_note_id}",
    responses={
        200: {"model": CreditNoteOut},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def update_credit_note(
    credit_note_id: UUID,
    payload: CreditNoteUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    async with AsyncSessionLocal() as session:
        try:
            lines_data = (
                [line.model_dump() for line in payload.lines]
                if payload.lines is not None
                else None
            )
            cn = await svc.api_update(
                session,
                credit_note_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                contact_id=payload.contact_id,
                issue_date=payload.issue_date,
                lines=lines_data,
                original_invoice_id=payload.original_invoice_id,
                reason=payload.reason,
                notes=payload.notes,
            )
        except svc.VersionConflict as exc:
            body = CreditNoteConflictBody(
                detail="version mismatch",
                current=CreditNoteOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.CreditNoteError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(cn)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{credit_note_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def void_credit_note(
    credit_note_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    async with AsyncSessionLocal() as session:
        try:
            await svc.api_void(
                session,
                credit_note_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            body = CreditNoteConflictBody(
                detail="version mismatch",
                current=CreditNoteOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.CreditNoteError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return Response(status_code=204)
