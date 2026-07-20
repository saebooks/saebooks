"""JSON router — ``/api/v1/stp-submissions``.

Read-only listing + preview of STP Phase 2 payloads. The actual
ATO submission lands in Phase 3.1. Payloads are auto-built by the
pay-run finalize flow; consumers can re-trigger via the dedicated
``POST /pay-runs/{id}/stp-event`` endpoint (on the pay-run router).

* List by company or by pay run
* Get individual payload for inspection
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.jurisdictions.au import stp as svc
from saebooks.models.stp_submission import StpSubmission

router = APIRouter(
    prefix="/stp-submissions",
    tags=["stp-submissions"],
    dependencies=[Depends(require_bearer)],
)


def _to_dto(sub: StpSubmission) -> dict[str, Any]:
    return {
        "id": str(sub.id),
        "company_id": str(sub.company_id),
        "pay_run_id": str(sub.pay_run_id),
        "event_type": sub.event_type,
        "status": sub.status,
        "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
        "ato_receipt_number": sub.ato_receipt_number,
        "errors": sub.errors or [],
        "totals": (sub.payload or {}).get("totals", {}),
        "payee_count": len((sub.payload or {}).get("payees", [])),
        "version": sub.version,
        "created_at": sub.created_at.isoformat(),
        "updated_at": sub.updated_at.isoformat(),
    }


@router.get("")
async def list_submissions(
    pay_run_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> dict[str, Any]:
    if pay_run_id is not None:
        items = await svc.list_for_pay_run(
            session, company_id=company_id, pay_run_id=pay_run_id
        )
        total = len(items)
    else:
        items, total = await svc.list_for_company(
            session, company_id=company_id, limit=limit, offset=offset
        )
    return {
        "items": [_to_dto(i) for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{submission_id}")
async def get_submission(
    submission_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> dict[str, Any]:
    sub = await session.get(StpSubmission, submission_id)
    if sub is None or sub.company_id != company_id:
        raise HTTPException(404, "stp submission not found")
    dto = _to_dto(sub)
    # Full payload only on single-record fetch.
    dto["payload"] = sub.payload
    return dto
