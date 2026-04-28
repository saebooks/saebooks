"""JSON router — ``/api/v1/journal_entries``.

Phase 1 tier-3 general ledger endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at) returning 204.
* Lines are nested in the response.
* Status transitions go through dedicated endpoints:
    POST /{id}/post    — DRAFT → POSTED
    POST /{id}/reverse — POSTED → REVERSED (creates mirror reversal entry)
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on transition endpoints.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; every query is gated by the ``tenant_isolation`` RLS
policy from migration 0055. Existence checks pass ``tenant_id`` to
``svc.get`` so a foreign-tenant UUID returns 404 even if the caller
knows the id.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    JournalEntryConflictBody,
    JournalEntryCreate,
    JournalEntryListOut,
    JournalEntryOut,
    JournalEntryPostBody,
    JournalEntryUpdate,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.models.journal import EntryStatus
from saebooks.services import journal_entries as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/journal_entries",
    tags=["journal_entries"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(entry: Any) -> dict[str, Any]:
    return json.loads(JournalEntryOut.model_validate(entry).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=JournalEntryListOut)
async def list_journal_entries(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalEntryListOut:
    offset = (page - 1) * page_size
    status_enum: EntryStatus | None = None
    if status is not None:
        try:
            status_enum = EntryStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
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
async def get_journal_entry(
    entry_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JournalEntryOut:
    tenant_id = resolve_tenant_id(request)
    entry = await svc.get(session, entry_id, tenant_id=tenant_id)
    if entry is None:
        raise HTTPException(404, "Journal entry not found")
    return JournalEntryOut.model_validate(entry)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=JournalEntryOut, status_code=201)
async def create_journal_entry(
    payload: JournalEntryCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
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
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, entry_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Journal entry not found")

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
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, entry_id, tenant_id=tenant_id)
    if existing is None:
        raise HTTPException(404, "Journal entry not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "journal_entries", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

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


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{entry_id}/post",
    responses={
        200: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
)
async def post_journal_entry(
    entry_id: UUID,
    request: Request,
    payload: JournalEntryPostBody = Body(default_factory=JournalEntryPostBody),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition journal entry DRAFT → POSTED.

    Checks period lock, auto-posts GST lines, verifies balance.
    Returns 422 if the entry is already POSTED or REVERSED.
    Returns 422 with "Period is locked" if entry_date falls in a locked period
    and no override_reason is supplied in the request body.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    idem_key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if idem_key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, idem_key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    if await svc.get(session, entry_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Journal entry not found")

    try:
        entry = await svc.api_post(
            session,
            entry_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            override_reason=payload.override_reason or None,
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
    if idem_key is not None:
        await store_response(session, idem_key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Reverse (POST /{id}/reverse → creates new reversal JE, marks original REVERSED)
# ---------------------------------------------------------------------------


@router.post(
    "/{entry_id}/reverse",
    responses={
        201: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
    status_code=201,
)
async def reverse_journal_entry(
    entry_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Create a reversal of a POSTED journal entry (POSTED → REVERSED).

    Creates a new JournalEntry with all debit/credit lines swapped,
    auto-posts it, and marks the original entry as REVERSED. The new
    reversal entry is returned. Only POSTED entries can be reversed;
    returns 422 for any other status.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    idem_key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if idem_key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, idem_key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    if await svc.get(session, entry_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Journal entry not found")

    try:
        reversal = await svc.api_reverse(
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

    body = _dump(reversal)
    if idem_key is not None:
        await store_response(session, idem_key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)
