"""JSON router — ``/api/v1/recurring_invoices``.

Phase 1 tier-4 recurring-invoice-template endpoint.

Recurring invoices are schedule + line templates used to spawn real invoices
on a cadence. This endpoint covers CRUD + listing only — invoice generation
(next_run recomputation, minting DRAFT invoices) is a separate concern.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
* Lines are replaced in full when ``lines`` key is present in a PATCH body.
  If absent, existing lines are untouched.
* Status lifecycle (ACTIVE → PAUSED / ENDED) is achieved via PATCH status.
  Archive is terminal and uses DELETE.
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
    RecurringInvoiceConflictBody,
    RecurringInvoiceCreate,
    RecurringInvoiceListOut,
    RecurringInvoiceOut,
    RecurringInvoiceUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import recurring_invoices as svc

router = APIRouter(
    prefix="/recurring_invoices",
    tags=["recurring_invoices"],
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


def _dump(ri: Any) -> dict[str, Any]:
    return json.loads(RecurringInvoiceOut.model_validate(ri).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=RecurringInvoiceListOut)
async def list_recurring_invoices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    contact_id: str | None = Query(default=None),
    frequency: str | None = Query(default=None),
    archived: bool = Query(default=False),
) -> RecurringInvoiceListOut:
    offset = (page - 1) * page_size
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        items, total = await svc.list_recurring_invoices(
            session,
            company_id,
            tenant_id,
            status=status,
            contact_id=contact_id,
            frequency=frequency,
            archived=archived,
            limit=page_size,
            offset=offset,
        )
        return RecurringInvoiceListOut(
            items=[RecurringInvoiceOut.model_validate(ri) for ri in items],
            total=total,
            limit=page_size,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{ri_id}", response_model=RecurringInvoiceOut)
async def get_recurring_invoice(ri_id: UUID) -> RecurringInvoiceOut:
    async with AsyncSessionLocal() as session:
        ri = await svc.api_get(session, ri_id)
        if ri is None:
            raise HTTPException(404, "Recurring invoice not found")
        return RecurringInvoiceOut.model_validate(ri)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=RecurringInvoiceOut, status_code=201)
async def create_recurring_invoice(
    payload: RecurringInvoiceCreate,
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
            ri = await svc.api_create(
                session,
                company_id,
                tenant_id,
                actor=f"api:{bearer[:8]}…",
                contact_id=payload.contact_id,
                name=payload.name,
                frequency=payload.frequency.value,
                next_run=payload.next_run,
                status=payload.status.value,
                anchor_day=payload.anchor_day,
                end_date=payload.end_date,
                due_days=payload.due_days,
                payment_terms=payload.payment_terms,
                notes=payload.notes,
                auto_post=payload.auto_post,
                lines=[ln.model_dump() for ln in payload.lines],
            )
        except (ValueError, svc.RecurringInvoiceApiError) as exc:
            raise HTTPException(422, str(exc)) from exc

        body = _dump(ri)
        if key is not None:
            await _remember_idempotent(session, key, body, 201)
            await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{ri_id}",
    responses={
        200: {"model": RecurringInvoiceOut},
        409: {"model": RecurringInvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def update_recurring_invoice(
    ri_id: UUID,
    payload: RecurringInvoiceUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        # Extract lines separately; pass remaining fields as kwargs.
        raw = payload.model_dump(exclude_unset=True)
        lines_payload = raw.pop("lines", None)
        # Convert lines dicts if present.
        if lines_payload is not None:
            lines_to_pass = [
                {k: (str(v) if hasattr(v, "__class__") and v.__class__.__name__ in ("UUID",) else v)
                 for k, v in ln.items()}
                for ln in lines_payload
            ]
        else:
            lines_to_pass = None

        # Convert enum values to their string values for the service.
        for enum_field in ("frequency", "status"):
            if enum_field in raw and raw[enum_field] is not None:
                raw[enum_field] = raw[enum_field].value

        try:
            ri = await svc.api_update(
                session,
                ri_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                lines=lines_to_pass,
                **raw,
            )
        except svc.VersionConflict as exc:
            body = RecurringInvoiceConflictBody(
                detail="version mismatch",
                current=RecurringInvoiceOut.model_validate(exc.current),
            ).model_dump(mode="json")
            if key is not None:
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.RecurringInvoiceApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(ri)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
            await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{ri_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": RecurringInvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def delete_recurring_invoice(
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")

    async with AsyncSessionLocal() as session:
        try:
            await svc.api_delete(
                session,
                ri_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            body = RecurringInvoiceConflictBody(
                detail="version mismatch",
                current=RecurringInvoiceOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.RecurringInvoiceApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return Response(status_code=204)
