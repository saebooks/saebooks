"""JSON router — ``/api/v1/journal_templates``.

Phase 1 cycle 40.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* No optimistic locking — ``JournalTemplate`` has no ``version`` column.
* PATCH and DELETE operate by ID with company_id isolation.
* ``POST /{id}/apply`` returns pre-filled lines for the caller to submit
  to ``/api/v1/journal_entries`` — it does NOT create a journal entry.
* The active company is resolved by ``get_active_company_id`` —
  callers may pin a specific company via ``X-Company-Id``; otherwise
  the first active company for the tenant is used.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    JournalTemplateApplyOut,
    JournalTemplateCreate,
    JournalTemplateLineOut,
    JournalTemplateListOut,
    JournalTemplateOut,
    JournalTemplateUpdate,
)
from saebooks.models.journal_template import JournalTemplate
from saebooks.services import journal_templates as svc
from saebooks.services.hard_delete import hard_delete_with_audit

router = APIRouter(
    prefix="/journal_templates",
    tags=["journal_templates"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dump(tmpl: JournalTemplate) -> dict[str, Any]:
    return json.loads(JournalTemplateOut.model_validate(tmpl).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=JournalTemplateListOut)
async def list_journal_templates(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalTemplateListOut:
    count_stmt = (
        select(func.count())
        .select_from(JournalTemplate)
        .where(
            JournalTemplate.company_id == company_id,
            JournalTemplate.archived_at.is_(None),
        )
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(JournalTemplate)
        .where(
            JournalTemplate.company_id == company_id,
            JournalTemplate.archived_at.is_(None),
        )
        .order_by(JournalTemplate.name)
        .offset(offset)
        .limit(limit)
    )
    templates = list((await session.execute(stmt)).scalars().all())

    return JournalTemplateListOut(
        items=[JournalTemplateOut.model_validate(t) for t in templates],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{template_id}", response_model=JournalTemplateOut)
async def get_journal_template(
    template_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalTemplateOut:
    tmpl = await svc.get(session, template_id)
    if tmpl is None or tmpl.company_id != company_id:
        raise HTTPException(404, "Journal template not found")
    return JournalTemplateOut.model_validate(tmpl)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=JournalTemplateOut, status_code=201)
async def create_journal_template(
    payload: JournalTemplateCreate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    try:
        tmpl = await svc.create(
            session,
            company_id,
            name=payload.name,
            description=payload.description,
            lines=[line.model_dump(mode="json") for line in payload.lines],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(tmpl)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH — no If-Match; template has no version column)
# ---------------------------------------------------------------------------


@router.patch("/{template_id}", response_model=JournalTemplateOut)
async def update_journal_template(
    template_id: UUID,
    payload: JournalTemplateUpdate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    # Verify the template exists and belongs to this company
    tmpl = await svc.get(session, template_id)
    if tmpl is None or tmpl.company_id != company_id:
        raise HTTPException(404, "Journal template not found")

    lines_data = (
        [line.model_dump(mode="json") for line in payload.lines]
        if payload.lines is not None
        else None
    )
    try:
        updated = await svc.update(
            session,
            template_id,
            name=payload.name,
            description=payload.description,
            lines=lines_data,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(updated)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive via archived_at → 204)
# ---------------------------------------------------------------------------


@router.delete("/{template_id}", responses={204: {"description": "Archived"}})
async def archive_journal_template(
    request: Request,
    template_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tmpl = await svc.get(session, template_id)
    if tmpl is None or tmpl.company_id != company_id:
        raise HTTPException(404, "Journal template not found")

    if hard:
        await hard_delete_with_audit(
            session, tmpl, "journal_templates", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    await svc.archive(session, template_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Apply — return pre-filled lines without creating a journal entry
# ---------------------------------------------------------------------------


@router.post("/{template_id}/apply", response_model=JournalTemplateApplyOut)
async def apply_journal_template(
    template_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalTemplateApplyOut:
    """Return a template's lines as suggested JE lines.

    The caller uses the returned ``suggested_lines`` as the ``lines`` array
    when POSTing to ``/api/v1/journal_entries``.  No journal entry is created
    by this endpoint.
    """
    tmpl = await svc.get(session, template_id)
    if tmpl is None or tmpl.company_id != company_id:
        raise HTTPException(404, "Journal template not found")
    if tmpl.archived_at is not None:
        raise HTTPException(422, "Template is archived and cannot be applied")

    suggested: list[JournalTemplateLineOut] = []
    for raw_line in tmpl.lines:
        acct_str = raw_line.get("account_id", "")
        try:
            account_id = uuid.UUID(acct_str) if acct_str else None
        except (ValueError, AttributeError):
            account_id = None

        tax_str = raw_line.get("tax_code_id", "")
        try:
            tax_code_id = uuid.UUID(tax_str) if tax_str else None
        except (ValueError, AttributeError):
            tax_code_id = None

        if account_id is None:
            continue  # skip lines with no account

        suggested.append(
            JournalTemplateLineOut(
                account_id=account_id,
                description=raw_line.get("description") or None,
                debit=Decimal(str(raw_line.get("debit", "0"))),
                credit=Decimal(str(raw_line.get("credit", "0"))),
                tax_code_id=tax_code_id,
            )
        )

    return JournalTemplateApplyOut(
        template_id=tmpl.id,
        template_name=tmpl.name,
        suggested_lines=suggested,
    )
