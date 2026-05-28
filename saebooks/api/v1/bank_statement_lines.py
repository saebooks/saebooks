"""JSON router — ``/api/v1/bank_statement_lines``.

Phase 1 tier-4 bank-statement-lines endpoint.

Individual transaction lines imported from bank statements.  Each line
belongs to a bank account (``accounts`` row where ``bsb IS NOT NULL``).

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
* List supports filters: ``bank_account_id``, ``status``, ``date_from``/``date_to``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    BankStatementLineConflictBody,
    BankStatementLineCreate,
    BankStatementLineListOut,
    BankStatementLineMatchRequest,
    BankStatementLineOut,
    BankStatementLineSplitMatchRequest,
    BankStatementLineUpdate,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.services import reconciliation as recon_svc
from saebooks.models.bank_statement import StatementLineStatus
from saebooks.services import bank_statement_lines as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/bank_statement_lines",
    tags=["bank_statement_lines"],
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
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(line: Any) -> dict[str, Any]:
    return json.loads(BankStatementLineOut.model_validate(line).model_dump_json())


def _parse_status(value: str | None) -> StatementLineStatus | None:
    if value is None:
        return None
    try:
        return StatementLineStatus(value.upper())
    except ValueError as exc:
        valid = ", ".join(s.value for s in StatementLineStatus)
        raise HTTPException(400, f"Invalid status '{value}'. Valid values: {valid}") from exc


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


_SORT_COLUMNS_PUBLIC = frozenset({"date", "description", "amount", "balance", "status", "reference"})


@router.get("", response_model=BankStatementLineListOut)
async def list_bank_statement_lines(
    request: Request,
    bank_account_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    sort: str = Query(default="date", description="One of: date, description, amount, balance, status, reference"),
    direction: str = Query(default="desc", regex="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankStatementLineListOut:
    status_filter = _parse_status(status)
    if sort not in _SORT_COLUMNS_PUBLIC:
        raise HTTPException(400, f"Invalid sort {sort!r}. Valid: {sorted(_SORT_COLUMNS_PUBLIC)}")
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        account_id=bank_account_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    return BankStatementLineListOut(
        items=[BankStatementLineOut.model_validate(ln) for ln in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{line_id}", response_model=BankStatementLineOut)
async def get_bank_statement_line(
    request: Request,
    line_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankStatementLineOut:
    tenant_id = resolve_tenant_id(request)
    line = await svc.api_get(session, line_id, tenant_id=tenant_id, company_id=company_id)
    if line is None or line.archived_at is not None:
        raise HTTPException(404, "Bank statement line not found")
    return BankStatementLineOut.model_validate(line)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BankStatementLineOut, status_code=201)
async def create_bank_statement_line(
    request: Request,
    payload: BankStatementLineCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)

    # Validate status value in payload
    status_val = _parse_status(payload.status) or StatementLineStatus.UNMATCHED

    tenant_id = resolve_tenant_id(request)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
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

    try:
        line = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            account_id=payload.account_id,
            txn_date=payload.txn_date,
            amount=payload.amount,
            description=payload.description,
            balance=payload.balance,
            reference=payload.reference,
            status=status_val,
            external_id=payload.external_id,
            bank_feed_account_id=payload.bank_feed_account_id,
            contact_id=payload.contact_id,
        )
    except (ValueError, svc.BankStatementLineError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(line)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{line_id}",
    responses={
        200: {"model": BankStatementLineOut},
        409: {"model": BankStatementLineConflictBody, "description": "Version mismatch"},
    },
)
async def update_bank_statement_line(
    request: Request,
    line_id: UUID,
    payload: BankStatementLineUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with line version is required")
    key = _parse_idempotency_key(idempotency_key)

    # Convert status string to enum if present
    update_kwargs = payload.model_dump(exclude_unset=True)
    if "status" in update_kwargs and update_kwargs["status"] is not None:
        update_kwargs["status"] = _parse_status(update_kwargs["status"])

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify line belongs to this tenant
    if await svc.api_get(session, line_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bank statement line not found")

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
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

    try:
        line = await svc.api_update(
            session,
            line_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **update_kwargs,
        )
    except svc.VersionConflict as exc:
        body = BankStatementLineConflictBody(
            detail="version mismatch",
            current=BankStatementLineOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankStatementLineError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(line)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{line_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": BankStatementLineConflictBody, "description": "Version mismatch"},
    },
)
async def delete_bank_statement_line(
    request: Request,
    line_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, line_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Bank statement line not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "bank_statement_lines", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with line version is required")

    try:
        await svc.api_delete(
            session,
            line_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = BankStatementLineConflictBody(
            detail="version mismatch",
            current=BankStatementLineOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankStatementLineError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Match (reconcile to a payment or journal entry)
# ---------------------------------------------------------------------------


@router.post("/{line_id}/match", response_model=BankStatementLineOut)
async def match_bank_statement_line(
    request: Request,
    line_id: UUID,
    payload: BankStatementLineMatchRequest,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Match a bank statement line to a payment or journal entry.

    Sets status=MATCHED, records matched_to_type, matched_to_id, matched_at,
    and bumps the version.
    """
    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, line_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bank statement line not found")

    try:
        line = await svc.api_match(
            session,
            line_id,
            actor=f"api:{bearer[:8]}…",
            matched_to_type=payload.matched_to_type.upper(),
            matched_to_id=payload.matched_to_id,
            tenant_id=tenant_id,
        )
    except (ValueError, svc.BankStatementLineError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump(line), status_code=200)


# ---------------------------------------------------------------------------
# Unmatch (clear reconciliation)
# ---------------------------------------------------------------------------


@router.post("/{line_id}/unmatch", response_model=BankStatementLineOut)
async def unmatch_bank_statement_line(
    request: Request,
    line_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Clear the reconciliation match on a bank statement line.

    Sets status=UNMATCHED and clears matched_to_type, matched_to_id,
    matched_entry_id, matched_at, matched_by.
    """
    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, line_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bank statement line not found")

    try:
        line = await svc.api_unmatch(
            session,
            line_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
        )
    except (ValueError, svc.BankStatementLineError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump(line), status_code=200)


# ---------------------------------------------------------------------------
# Split-match (ETSY-4 — net-of-fee Stripe payouts)
# ---------------------------------------------------------------------------


@router.post("/{line_id}/split_match", response_model=BankStatementLineOut)
async def split_match_bank_statement_line(
    request: Request,
    line_id: UUID,
    payload: BankStatementLineSplitMatchRequest,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Match a bank statement line via a new split journal entry.

    Creates a balanced journal entry whose bank-account side equals the BSL
    amount and whose allocation side is provided by the caller, then posts the
    entry and marks the BSL as MATCHED.

    The allocations list must satisfy:
      sum(credit) - sum(debit) == bank_line.amount

    Example — $970 Stripe net payout:
      allocations = [
        {"account_id": "<AR>",           "credit": 1000, "debit": 0},
        {"account_id": "<StripeFees>",   "debit": 30,    "credit": 0}
      ]
    """
    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, line_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Bank statement line not found")

    alloc_dicts = [
        {
            "account_id": a.account_id,
            "debit": a.debit,
            "credit": a.credit,
            "description": a.description,
            "tax_code_id": a.tax_code_id,
        }
        for a in payload.allocations
    ]

    try:
        line = await recon_svc.split_match_line(
            session,
            line_id,
            company_id=company_id,
            allocations=alloc_dicts,
            entry_date=payload.entry_date,
            description=payload.description,
            posted_by=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc

    return JSONResponse(_dump(line), status_code=200)
