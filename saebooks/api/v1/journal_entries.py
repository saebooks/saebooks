"""JSON router — ``/api/v1/journal_entries``.

Phase 1 tier-3 general ledger endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at) returning 204.
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
    JournalEntryConflictBody,
    JournalEntryCreate,
    JournalEntryListOut,
    JournalEntryOut,
    JournalEntryUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.services import journal_entries as svc

router = APIRouter(
    prefix="/journal_entries",
    tags=["journal_entries"],
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
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _dump(entry: Any) -> dict[str, Any]:
    return json.loads(JournalEntryOut.model_validate(entry).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=JournalEntryListOut)
async def list_journal_entries(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> JournalEntryListOut:
    offset = (page - 1) * page_size
    status_enum: EntryStatus | None = None
    if status is not None:
        try:
            status_enum = EntryStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        entries, total = await svc.list_active(
            session,
            company_id,
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            status=status_enum,
            limit=page_size,
            offset=offset,
        )
        return JournalEntryListOut(
            items=[JournalEntryOut.model_validate(e) for e in entries],
            total=total,
            limit=page_size,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{entry_id}", response_model=JournalEntryOut)
async def get_journal_entry(entry_id: UUID) -> JournalEntryOut:
    async with AsyncSessionLocal() as session:
        entry = await svc.get(session, entry_id)
        if entry is None:
            raise HTTPException(404, "Journal entry not found")
        return JournalEntryOut.model_validate(entry)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=JournalEntryOut, status_code=201)
async def create_journal_entry(
    payload: JournalEntryCreate,
    bearer: str = Depends(require_bearer),
) -> Any:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        try:
            entry = await svc.create(
                session,
                company_id,
                tenant_id,
                actor=f"api:{bearer[:8]}…",
                entry_date=payload.entry_date,
                narration=payload.narration,
                reference=payload.reference,
                lines=[line.model_dump() for line in payload.lines],
            )
        except (ValueError, svc.JournalEntryError) as exc:
            raise HTTPException(422, str(exc)) from exc

        body = _dump(entry)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{entry_id}",
    responses={
        200: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
)
async def update_journal_entry(
    entry_id: UUID,
    payload: JournalEntryUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    async with AsyncSessionLocal() as session:
        try:
            lines_data = (
                [line.model_dump() for line in payload.lines]
                if payload.lines is not None
                else None
            )
            entry = await svc.update(
                session,
                entry_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                entry_date=payload.entry_date,
                narration=payload.narration,
                reference=payload.reference,
                status=payload.status,
                lines=lines_data,
            )
        except svc.VersionConflict as exc:
            body = JournalEntryConflictBody(
                detail="version mismatch",
                current=JournalEntryOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.JournalEntryError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(entry)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{entry_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
)
async def void_journal_entry(
    entry_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    async with AsyncSessionLocal() as session:
        try:
            await svc.void(
                session,
                entry_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            body = JournalEntryConflictBody(
                detail="version mismatch",
                current=JournalEntryOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.JournalEntryError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return Response(status_code=204)
