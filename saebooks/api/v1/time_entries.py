"""JSON router — ``/api/v1/time-entries``.

Standalone time-tracking v1 endpoints:

* Bearer auth via ``require_bearer``.
* Optimistic locking via ``If-Match: <version>`` on update/delete/workflow.
* DELETE is a soft-archive (only allowed in DRAFT/REJECTED).
* Workflow actions: submit, approve, reject (state machine in service).
* Conversion: POST /convert-to-invoice — bundles N billable entries
  into one invoice line on a DRAFT invoice.
* Helper: GET /week?week_start=YYYY-MM-DD&user_id=... — pre-bucketed
  entries for the weekly grid UX (the killer feature).
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    TimeEntryConvertToInvoiceLineRequest,
    TimeEntryConvertToInvoiceLineResponse,
    TimeEntryCreate,
    TimeEntryListOut,
    TimeEntryOut,
    TimeEntryUpdate,
)
from saebooks.models.time_entry import TimeEntry, TimeEntryApprovalStatus
from saebooks.services import time_entries as svc
from saebooks.services.time_entries import TimeEntryError, TimeEntryFilters

router = APIRouter(
    prefix="/time-entries",
    tags=["time-entries"],
    dependencies=[Depends(require_bearer)],
)


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


def _dump(entry: TimeEntry) -> dict[str, Any]:
    return json.loads(TimeEntryOut.model_validate(entry, from_attributes=True).model_dump_json())


def _resolve_user_id(request: Request, supplied: uuid.UUID | None) -> uuid.UUID:
    """Pick user_id for a new entry.

    Priority:
      1. explicitly supplied (admins logging another worker's time)
      2. request.state.user (JWT-authenticated session)
      3. error — dev-token caller MUST supply user_id explicitly
    """
    if supplied is not None:
        return supplied
    state_user = getattr(request.state, "user", None)
    if state_user is not None and getattr(state_user, "id", None):
        return state_user.id
    raise HTTPException(
        400,
        "user_id required (dev-token / unauthenticated caller — pass user_id in body)",
    )


def _translate_error(exc: TimeEntryError) -> HTTPException:
    """Map domain errors to HTTP. Default 409 for state conflicts,
    400 for input errors, 404 for not-found.
    """
    if exc.code in {"version_mismatch"}:
        return HTTPException(412, str(exc))
    if exc.code in {
        "entries_not_found",
        "invoice_not_found",
        "contact_not_found",
        "no_income_account",
    }:
        return HTTPException(404, str(exc))
    if exc.code in {
        "wrong_state",
        "not_editable",
        "not_archivable",
        "already_converted",
        "locked",
        "invoice_not_draft",
    }:
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# List + weekly grid
# ---------------------------------------------------------------------------


@router.get("", response_model=TimeEntryListOut)
async def list_time_entries(
    request: Request,
    user_id: uuid.UUID | None = Query(default=None),
    contact_id: uuid.UUID | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    approval_status: str | None = Query(default=None),
    billable_only: bool = Query(default=False),
    uninvoiced_only: bool = Query(default=False),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> TimeEntryListOut:
    items, total = await svc.list_entries(
        session,
        company_id=company_id,
        filters=TimeEntryFilters(
            user_id=user_id,
            contact_id=contact_id,
            project_id=project_id,
            approval_status=approval_status,
            billable_only=billable_only,
            uninvoiced_only=uninvoiced_only,
            date_from=date_from,
            date_to=date_to,
        ),
        limit=limit,
        offset=offset,
    )
    return TimeEntryListOut(
        items=[TimeEntryOut.model_validate(e, from_attributes=True) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/week", response_model=list[TimeEntryOut])
async def list_week(
    request: Request,
    week_start: date = Query(...),
    user_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> list[TimeEntryOut]:
    """Return one week of entries for the weekly-grid view.

    Defaults to the authenticated user; admins can pass ``user_id``
    to view someone else's week.
    """
    target_user_id = _resolve_user_id(request, user_id)
    rows = await svc.list_week(
        session,
        company_id=company_id,
        user_id=target_user_id,
        week_start=week_start,
    )
    return [TimeEntryOut.model_validate(e, from_attributes=True) for e in rows]


# ---------------------------------------------------------------------------
# Get / create / update / archive
# ---------------------------------------------------------------------------


@router.get("/{entry_id}", response_model=TimeEntryOut)
async def get_time_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> TimeEntryOut:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    return TimeEntryOut.model_validate(entry, from_attributes=True)


@router.post("", response_model=TimeEntryOut, status_code=201)
async def create_time_entry(
    request: Request,
    body: TimeEntryCreate,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    user_id = _resolve_user_id(request, body.user_id)
    tenant_id = resolve_tenant_id(request)
    try:
        entry = await svc.create(
            session,
            company_id=company_id,
            user_id=user_id,
            work_date=body.work_date,
            hours=body.hours,
            description=body.description,
            contact_id=body.contact_id,
            project_id=body.project_id,
            department_id=body.department_id,
            cost_centre_id=body.cost_centre_id,
            start_time=body.start_time,
            end_time=body.end_time,
            break_minutes=body.break_minutes,
            billable=body.billable,
            rate=body.rate,
            tenant_id=tenant_id,
        )
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _dump(entry),
        status_code=201,
        headers={"ETag": f'"{entry.version}"'},
    )


@router.patch("/{entry_id}", response_model=TimeEntryOut)
async def update_time_entry(
    entry_id: uuid.UUID,
    body: TimeEntryUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    expected_version = _parse_if_match(if_match)

    # Build kwargs from non-None body fields.
    fields = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
    }
    try:
        entry = await svc.update(
            session,
            entry=entry,
            expected_version=expected_version,
            **fields,
        )
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _dump(entry),
        status_code=200,
        headers={"ETag": f'"{entry.version}"'},
    )


@router.delete("/{entry_id}", status_code=204)
async def archive_time_entry(
    entry_id: uuid.UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Response:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    if hard:
        # Developer-tier hard delete — bypass the state-machine archive rules
        # entirely; remove the row + cascade dependents via the audit helper.
        from saebooks.services.hard_delete import hard_delete_with_audit
        await hard_delete_with_audit(
            session, entry, "time_entries", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and entry.version != expected_version:
        raise HTTPException(412, "version mismatch")
    try:
        await svc.archive(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Workflow: submit / approve / reject
# ---------------------------------------------------------------------------


@router.post("/{entry_id}/submit", response_model=TimeEntryOut)
async def submit_time_entry(
    entry_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and entry.version != expected_version:
        raise HTTPException(412, "version mismatch")
    try:
        entry = await svc.submit(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(_dump(entry), headers={"ETag": f'"{entry.version}"'})


@router.post("/{entry_id}/approve", response_model=TimeEntryOut)
async def approve_time_entry(
    entry_id: uuid.UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and entry.version != expected_version:
        raise HTTPException(412, "version mismatch")
    approver_id = _resolve_user_id(request, None)
    try:
        entry = await svc.approve(session, entry=entry, approver_user_id=approver_id)
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(_dump(entry), headers={"ETag": f'"{entry.version}"'})


@router.post("/{entry_id}/reject", response_model=TimeEntryOut)
async def reject_time_entry(
    entry_id: uuid.UUID,
    request: Request,
    reason: str = Body(..., embed=True),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and entry.version != expected_version:
        raise HTTPException(412, "version mismatch")
    approver_id = _resolve_user_id(request, None)
    try:
        entry = await svc.reject(
            session,
            entry=entry,
            approver_user_id=approver_id,
            reason=reason,
        )
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(_dump(entry), headers={"ETag": f'"{entry.version}"'})


# ---------------------------------------------------------------------------
# Convert billable entries to an invoice line
# ---------------------------------------------------------------------------


@router.post("/{entry_id}/revert", response_model=TimeEntryOut)
async def revert_time_entry(
    entry_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Revert APPROVED -> DRAFT so the entry can be edited or archived."""
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and entry.version != expected_version:
        raise HTTPException(412, "version mismatch")
    try:
        entry = await svc.revert(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(_dump(entry), headers={"ETag": f'"{entry.version}"'})


@router.post(
    "/convert-to-invoice",
    response_model=TimeEntryConvertToInvoiceLineResponse,
    status_code=201,
)
async def convert_to_invoice(
    request: Request,
    body: TimeEntryConvertToInvoiceLineRequest,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> TimeEntryConvertToInvoiceLineResponse:
    try:
        result = await svc.convert_to_invoice_line(
            session,
            company_id=company_id,
            entry_ids=body.entry_ids,
            invoice_id=body.invoice_id,
            contact_id=body.contact_id,
            tenant_id=resolve_tenant_id(request),
        )
        await session.commit()
    except TimeEntryError as exc:
        raise _translate_error(exc) from exc
    return TimeEntryConvertToInvoiceLineResponse(
        invoice_id=result.invoice_id,
        invoice_line_id=result.invoice_line_id,
        converted_entry_ids=result.converted_entry_ids,
        total_hours=result.total_hours,
        total_amount=result.total_amount,
    )
