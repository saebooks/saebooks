"""JSON router — ``/api/v1/reclassifications``.

The first-class Reclassification record type (Gap 2,
``saebooks-0157-builder-prompt.md``). A reclassification moves an
already-posted ``amount`` from one account to another (typically into a child
account) by posting ONE balanced, engine-generated reclass journal entry —
WITHOUT mutating the original posted entry. It is the lightweight,
audit-preserving alternative to void+recreate for a pure classification change
(the ~983 posted expenses moving into child expense accounts).

Endpoints (mirrors the transfers surface):

* ``POST   /reclassifications``            — create + post a reclassification.
* ``GET    /reclassifications``            — list for the active company.
* ``GET    /reclassifications/{id}``       — single reclassification.
* ``POST   /reclassifications/{id}/reverse`` — reverse (posts the mirror JE,
  flips status to REVERSED).

Auth: standard Bearer (``require_bearer``). ``tenant_id`` resolves from the
JWT claims; ``company_id`` from ``X-Company-Id`` (or first active company via
``get_active_company_id``).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.journal_entries import _resolve_actor_role
from saebooks.models.reclassification import Reclassification
from saebooks.services import reclassifications as svc

router = APIRouter(
    prefix="/reclassifications",
    tags=["reclassifications"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ReclassificationCreate(BaseModel):
    """POST /reclassifications body.

    Moves ``amount`` from ``from_account_id`` to ``to_account_id``. Both must
    be accounts of the active company on the SAME natural balance side
    (expense->expense, asset->asset, income->income, liability->liability).
    The canonical use is a same-parent child move or an account correction.
    ``source_entry_id`` is the original JE being reclassified, for traceability
    only — it is never mutated. ``override_reason`` is required to post into a
    locked period (governed by the same gate as every other post).
    """

    from_account_id: UUID
    to_account_id: UUID
    amount: Decimal = Field(gt=Decimal("0"))
    reclass_date: date
    reason: str | None = Field(default=None, max_length=1000)
    source_entry_id: UUID | None = None
    override_reason: str | None = Field(default=None, max_length=1000)


class ReclassificationReverse(BaseModel):
    """POST /reclassifications/{id}/reverse body."""

    reversal_date: date | None = None
    override_reason: str | None = Field(default=None, max_length=1000)


class ReclassificationOut(BaseModel):
    id: UUID
    company_id: UUID
    from_account_id: UUID
    to_account_id: UUID
    amount: Decimal
    reclass_date: date
    reason: str | None
    source_entry_id: UUID | None
    journal_entry_id: UUID | None
    status: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReclassificationListOut(BaseModel):
    items: list[ReclassificationOut]


def _to_out(r: Reclassification) -> ReclassificationOut:
    return ReclassificationOut.model_validate(r)


# ---------------------------------------------------------------------------
# POST /reclassifications
# ---------------------------------------------------------------------------


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ReclassificationOut
)
async def create_reclassification(
    payload: ReclassificationCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ReclassificationOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    actor_role = _resolve_actor_role(request)
    try:
        reclass = await svc.create_and_post_reclassification(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            from_account_id=payload.from_account_id,
            to_account_id=payload.to_account_id,
            amount=payload.amount,
            reclass_date=payload.reclass_date,
            reason=payload.reason,
            source_entry_id=payload.source_entry_id,
            created_by=str(actor),
            override_reason=payload.override_reason,
            actor_role=actor_role,
        )
    except svc.ReclassificationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "reclassification_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(reclass)


# ---------------------------------------------------------------------------
# GET /reclassifications
# ---------------------------------------------------------------------------


@router.get("", response_model=ReclassificationListOut)
async def list_reclassifications(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    account_id: UUID | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> ReclassificationListOut:
    tenant_id = resolve_tenant_id(request)
    offset = (page - 1) * page_size
    rows = await svc.list_reclassifications(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return ReclassificationListOut(items=[_to_out(r) for r in rows])


# ---------------------------------------------------------------------------
# GET /reclassifications/{id}
# ---------------------------------------------------------------------------


@router.get("/{reclassification_id}", response_model=ReclassificationOut)
async def get_reclassification(
    reclassification_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ReclassificationOut:
    tenant_id = resolve_tenant_id(request)
    try:
        reclass = await svc.get_reclassification(
            session,
            reclassification_id,
            tenant_id=tenant_id,
            company_id=company_id,
        )
    except svc.ReclassificationError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "reclassification_not_found", "detail": str(exc)},
        ) from exc
    return _to_out(reclass)


# ---------------------------------------------------------------------------
# POST /reclassifications/{id}/reverse
# ---------------------------------------------------------------------------


@router.post(
    "/{reclassification_id}/reverse", response_model=ReclassificationOut
)
async def reverse_reclassification(
    reclassification_id: UUID,
    request: Request,
    payload: ReclassificationReverse | None = None,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ReclassificationOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    actor_role = _resolve_actor_role(request)
    reversal_date = payload.reversal_date if payload is not None else None
    override_reason = payload.override_reason if payload is not None else None
    try:
        reclass = await svc.reverse_reclassification(
            session,
            reclassification_id,
            tenant_id=tenant_id,
            company_id=company_id,
            reversal_date=reversal_date,
            posted_by=str(actor),
            override_reason=override_reason,
            actor_role=actor_role,
        )
    except svc.ReclassificationError as exc:
        # "not found" -> 404; everything else (already reversed, post fail) -> 409.
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "reclassification_not_found", "detail": msg},
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "reclassification_not_reversible", "detail": msg},
        ) from exc
    return _to_out(reclass)
