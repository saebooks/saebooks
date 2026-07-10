"""Module routes for time entries — thin shell over ``services.time_entries``.

Differences from the quotes/PO routers:
* time-entries service has NO ``VersionConflict`` — it raises ``TimeEntryError``
  (with a ``code``); the module returns 422 ``{"code","message"}`` and the
  facade re-raises ``TimeEntryError`` so the engine router's ``_translate_error``
  handles it unchanged.
* the mutators take the ORM ``entry`` object, and the service does NOT commit
  (the caller does). The module therefore re-fetches by id and commits itself,
  exactly as the engine router does.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from preaccounting_app.deps import (
    TenantContext,
    get_module_session,
    get_tenant_context,
    require_preaccounting_token,
)
from saebooks.api.v1.schemas import TimeEntryOut, TimeEntryUpdate
from saebooks.services import time_entries as svc
from saebooks.services.time_entries import TimeEntryError, TimeEntryFilters

router = APIRouter(
    prefix="/time-entries",
    tags=["preaccounting-time-entries"],
    dependencies=[Depends(require_preaccounting_token)],
)


def _dump(entry: Any) -> dict[str, Any]:
    return json.loads(
        TimeEntryOut.model_validate(entry, from_attributes=True).model_dump_json()
    )


def _domain_error(exc: TimeEntryError) -> JSONResponse:
    return JSONResponse(
        {"code": getattr(exc, "code", "time_entry_error"), "message": str(exc)},
        status_code=422,
    )


async def _load(session: AsyncSession, company_id: uuid.UUID, entry_id: uuid.UUID):
    entry = await svc.get(session, company_id=company_id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(404, "time entry not found")
    return entry


class GetBody(BaseModel):
    entry_id: uuid.UUID


@router.post("/get")
async def get_entry(
    body: GetBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "get requires X-Company-Id"
    entry = await svc.get(session, company_id=ctx.company_id, entry_id=body.entry_id)
    return JSONResponse(_dump(entry) if entry is not None else None)


class ListBody(BaseModel):
    user_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    approval_status: str | None = None
    billable_only: bool = False
    uninvoiced_only: bool = False
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 100
    offset: int = 0


@router.post("/list")
async def list_entries(
    body: ListBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "list requires X-Company-Id"
    items, total = await svc.list_entries(
        session,
        company_id=ctx.company_id,
        filters=TimeEntryFilters(
            user_id=body.user_id,
            contact_id=body.contact_id,
            project_id=body.project_id,
            approval_status=body.approval_status,
            billable_only=body.billable_only,
            uninvoiced_only=body.uninvoiced_only,
            date_from=body.date_from,
            date_to=body.date_to,
        ),
        limit=body.limit,
        offset=body.offset,
    )
    return JSONResponse({"items": [_dump(e) for e in items], "total": total})


class ListWeekBody(BaseModel):
    user_id: uuid.UUID
    week_start: date


@router.post("/list-week")
async def list_week(
    body: ListWeekBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "list-week requires X-Company-Id"
    rows = await svc.list_week(
        session,
        company_id=ctx.company_id,
        user_id=body.user_id,
        week_start=body.week_start,
    )
    return JSONResponse({"items": [_dump(e) for e in rows]})


class CreateBody(BaseModel):
    user_id: uuid.UUID
    work_date: date
    hours: Decimal
    description: str = ""
    contact_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int = 0
    billable: bool = False
    rate: Decimal | None = None


@router.post("/create")
async def create_entry(
    body: CreateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "create requires X-Company-Id"
    try:
        entry = await svc.create(
            session,
            company_id=ctx.company_id,
            user_id=body.user_id,
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
            tenant_id=ctx.tenant_id,
        )
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry), status_code=201)


class UpdateBody(BaseModel):
    entry_id: uuid.UUID
    expected_version: int | None = None
    force: bool = False
    fields: dict[str, Any] = {}


@router.post("/update")
async def update_entry(
    body: UpdateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "update requires X-Company-Id"
    # Re-coerce the delegated field dict to proper Python types while
    # preserving exactly which keys were set (so a PATCH clearing an FK to
    # None survives). Constructing TimeEntryUpdate from only the set keys and
    # dumping exclude_unset reproduces the engine router's `fields` mapping.
    typed = TimeEntryUpdate(**body.fields).model_dump(exclude_unset=True)
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.update(
            session,
            entry=entry,
            expected_version=body.expected_version,
            force=body.force,
            **typed,
        )
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


class MutateBody(BaseModel):
    entry_id: uuid.UUID


@router.post("/archive")
async def archive_entry(
    body: MutateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.archive(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


@router.post("/submit")
async def submit_entry(
    body: MutateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.submit(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


@router.post("/revert")
async def revert_entry(
    body: MutateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.revert(session, entry=entry)
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


class ApproveBody(BaseModel):
    entry_id: uuid.UUID
    approver_user_id: uuid.UUID


@router.post("/approve")
async def approve_entry(
    body: ApproveBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.approve(
            session, entry=entry, approver_user_id=body.approver_user_id
        )
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


class RejectBody(BaseModel):
    entry_id: uuid.UUID
    approver_user_id: uuid.UUID
    reason: str


@router.post("/reject")
async def reject_entry(
    body: RejectBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None
    entry = await _load(session, ctx.company_id, body.entry_id)
    try:
        entry = await svc.reject(
            session,
            entry=entry,
            approver_user_id=body.approver_user_id,
            reason=body.reason,
        )
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(entry))


class ConvertBody(BaseModel):
    entry_ids: list[uuid.UUID]
    invoice_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


@router.post("/convert-to-invoice-line")
async def convert_to_invoice_line(
    body: ConvertBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "convert requires X-Company-Id"
    try:
        result = await svc.convert_to_invoice_line(
            session,
            company_id=ctx.company_id,
            entry_ids=body.entry_ids,
            invoice_id=body.invoice_id,
            contact_id=body.contact_id,
            tenant_id=ctx.tenant_id,
        )
        await session.commit()
    except TimeEntryError as exc:
        return _domain_error(exc)
    return JSONResponse(
        {
            "invoice_id": str(result.invoice_id),
            "invoice_line_id": str(result.invoice_line_id),
            "converted_entry_ids": [str(e) for e in result.converted_entry_ids],
            "total_hours": str(result.total_hours),
            "total_amount": str(result.total_amount),
        }
    )
