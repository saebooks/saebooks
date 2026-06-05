"""JSON router — ``/api/v1/transfers``.

The first-class Transfer (account-to-account money movement) record type —
DB-rebuild handover #2. A transfer moves money between two balance-sheet
accounts of ONE company (bank -> credit-card paydown, bank -> director-loan
repayment, bank/loan transfer) and compiles to ONE balance-sheet journal entry
(Dr to / Cr from, no GST) via ``services/transfers.py``. Replaces the
spend-money-Expense-to-a-liability stopgap; underpins directors-loan
traceability.

Endpoints (mirrors the payments/expenses surface):

* ``POST   /transfers``            — create + post a transfer.
* ``GET    /transfers``            — list transfers for the active company.
* ``GET    /transfers/{id}``       — single transfer.
* ``POST   /transfers/{id}/reverse`` — void/reverse a transfer (posts the
  mirror JE, flips status to REVERSED).

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
from saebooks.models.transfer import Transfer
from saebooks.services import transfers as svc

router = APIRouter(
    prefix="/transfers",
    tags=["transfers"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TransferCreate(BaseModel):
    """POST /transfers body.

    Money LEAVES ``from_account_id`` and ARRIVES at ``to_account_id``. Both
    must be balance-sheet accounts of the active company. Examples:
    credit-card paydown (from=bank, to=2-1115); director-loan repayment
    (from=bank, to=2-2200); bank/loan transfer (from=bank A, to=bank B).
    """

    from_account_id: UUID
    to_account_id: UUID
    amount: Decimal = Field(gt=Decimal("0"))
    transfer_date: date
    description: str | None = Field(default=None, max_length=500)
    reference: str | None = Field(default=None, max_length=64)


class TransferReverse(BaseModel):
    """POST /transfers/{id}/reverse body."""

    reversal_date: date | None = None


class TransferOut(BaseModel):
    id: UUID
    company_id: UUID
    from_account_id: UUID
    to_account_id: UUID
    amount: Decimal
    transfer_date: date
    description: str | None
    reference: str | None
    status: str
    journal_entry_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TransferListOut(BaseModel):
    items: list[TransferOut]


def _to_out(t: Transfer) -> TransferOut:
    return TransferOut.model_validate(t)


# ---------------------------------------------------------------------------
# POST /transfers
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TransferOut)
async def create_transfer(
    payload: TransferCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TransferOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        transfer = await svc.create_and_post_transfer(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            from_account_id=payload.from_account_id,
            to_account_id=payload.to_account_id,
            amount=payload.amount,
            transfer_date=payload.transfer_date,
            description=payload.description,
            reference=payload.reference,
            posted_by=str(actor),
        )
    except svc.TransferError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "transfer_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(transfer)


# ---------------------------------------------------------------------------
# GET /transfers
# ---------------------------------------------------------------------------


@router.get("", response_model=TransferListOut)
async def list_transfers(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    account_id: UUID | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> TransferListOut:
    tenant_id = resolve_tenant_id(request)
    offset = (page - 1) * page_size
    rows = await svc.list_transfers(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return TransferListOut(items=[_to_out(t) for t in rows])


# ---------------------------------------------------------------------------
# GET /transfers/{id}
# ---------------------------------------------------------------------------


@router.get("/{transfer_id}", response_model=TransferOut)
async def get_transfer(
    transfer_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TransferOut:
    tenant_id = resolve_tenant_id(request)
    try:
        transfer = await svc.get_transfer(
            session, transfer_id, tenant_id=tenant_id, company_id=company_id
        )
    except svc.TransferError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "transfer_not_found", "detail": str(exc)},
        ) from exc
    return _to_out(transfer)


# ---------------------------------------------------------------------------
# POST /transfers/{id}/reverse
# ---------------------------------------------------------------------------


@router.post("/{transfer_id}/reverse", response_model=TransferOut)
async def reverse_transfer(
    transfer_id: UUID,
    request: Request,
    payload: TransferReverse | None = None,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TransferOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    reversal_date = payload.reversal_date if payload is not None else None
    try:
        transfer = await svc.reverse_transfer(
            session,
            transfer_id,
            tenant_id=tenant_id,
            company_id=company_id,
            reversal_date=reversal_date,
            posted_by=str(actor),
        )
    except svc.TransferError as exc:
        # "not found" -> 404; everything else (already reversed, post fail) -> 409.
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "transfer_not_found", "detail": msg},
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "transfer_not_reversible", "detail": msg},
        ) from exc
    return _to_out(transfer)
