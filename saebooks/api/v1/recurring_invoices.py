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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    InvoiceOut,
    RecurringInvoiceConflictBody,
    RecurringInvoiceCreate,
    RecurringInvoiceGenerateResponse,
    RecurringInvoiceListOut,
    RecurringInvoiceOut,
    RecurringInvoiceUpdate,
)
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.models.recurring_invoice import RecurrenceStatus
from saebooks.services import recurrence as recurrence_svc
from saebooks.services import recurring_invoices as svc

router = APIRouter(
    prefix="/recurring_invoices",
    tags=["recurring_invoices"],
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
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    contact_id: str | None = Query(default=None),
    frequency: str | None = Query(default=None),
    archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> RecurringInvoiceListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
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
async def get_recurring_invoice(
    request: Request,
    ri_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> RecurringInvoiceOut:
    tenant_id = resolve_tenant_id(request)
    ri = await svc.get(session, ri_id, tenant_id=tenant_id)
    if ri is None:
        raise HTTPException(404, "Recurring invoice not found")
    return RecurringInvoiceOut.model_validate(ri)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=RecurringInvoiceOut, status_code=201)
async def create_recurring_invoice(
    request: Request,
    payload: RecurringInvoiceCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    if key is not None:
        replay = await _idempotent_replay(session, key)
        if replay is not None:
            return replay

    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    try:
        ri = await svc.create(
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
    request: Request,
    ri_id: UUID,
    payload: RecurringInvoiceUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify template belongs to this tenant
    if await svc.get(session, ri_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Recurring invoice not found")

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
        ri = await svc.update(
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
    request: Request,
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, ri_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Recurring invoice not found")

    try:
        await svc.delete(
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


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


@router.post(
    "/{ri_id}/pause",
    responses={
        200: {"model": RecurringInvoiceOut},
        409: {"model": RecurringInvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def pause_recurring_invoice(
    request: Request,
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition ACTIVE → PAUSED."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, ri_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Recurring invoice not found")

    try:
        ri = await svc.pause(
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

    return JSONResponse(_dump(ri), status_code=200)


@router.post(
    "/{ri_id}/resume",
    responses={
        200: {"model": RecurringInvoiceOut},
        409: {"model": RecurringInvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def resume_recurring_invoice(
    request: Request,
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition PAUSED → ACTIVE."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, ri_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Recurring invoice not found")

    try:
        ri = await svc.resume(
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

    return JSONResponse(_dump(ri), status_code=200)


@router.post(
    "/{ri_id}/end",
    responses={
        200: {"model": RecurringInvoiceOut},
        409: {"model": RecurringInvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def end_recurring_invoice(
    request: Request,
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition any non-ENDED status → ENDED (terminal)."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, ri_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Recurring invoice not found")

    try:
        ri = await svc.end(
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

    return JSONResponse(_dump(ri), status_code=200)


# ---------------------------------------------------------------------------
# Manual invoice generation
# ---------------------------------------------------------------------------


@router.post(
    "/{ri_id}/generate",
    response_model=RecurringInvoiceGenerateResponse,
    status_code=201,
    responses={
        201: {"model": RecurringInvoiceGenerateResponse},
        409: {"description": "Version mismatch (stale If-Match)"},
        422: {"description": "Template not ACTIVE or generation error"},
    },
)
async def generate_invoice(
    request: Request,
    ri_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Manually materialise one invoice from an ACTIVE recurring template.

    Requires ``If-Match: <version>`` for optimistic locking — if the
    template has been modified since the client last fetched it, 409 is
    returned. Provide ``X-Idempotency-Key`` to make the call retry-safe.

    Only ACTIVE templates may be triggered. PAUSED, ENDED, or archived
    templates return 422. Generation uses the template's ``next_run`` as
    the issue date so the cadence advances correctly.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with template version is required")
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        replay = await _idempotent_replay(session, key)
        if replay is not None:
            return replay

    tenant_id = resolve_tenant_id(request)
    ri = await svc.get(session, ri_id, tenant_id=tenant_id)
    if ri is None:
        raise HTTPException(404, "Recurring invoice not found")

    if ri.version != expected:
        body = RecurringInvoiceConflictBody(
            detail="version mismatch",
            current=RecurringInvoiceOut.model_validate(ri),
        ).model_dump(mode="json")
        if key is not None:
            await _remember_idempotent(session, key, body, 409)
            await session.commit()
        return JSONResponse(body, status_code=409)

    if ri.archived_at is not None:
        raise HTTPException(422, "Cannot generate from an archived recurring invoice")

    if ri.status != RecurrenceStatus.ACTIVE:
        raise HTTPException(
            422,
            f"Cannot generate: recurring invoice status is {ri.status.value!r}, expected ACTIVE",
        )

    try:
        invoice = await recurrence_svc.materialise_one(
            session, ri, as_of=ri.next_run
        )
    except recurrence_svc.RecurrenceError as exc:
        raise HTTPException(422, str(exc)) from exc

    invoice_body = json.loads(InvoiceOut.model_validate(invoice).model_dump_json())
    body = {
        "invoice_id": str(invoice.id),
        "invoice": invoice_body,
    }
    if key is not None:
        await _remember_idempotent(session, key, body, 201)
        await session.commit()

    return JSONResponse(body, status_code=201)
