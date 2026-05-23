"""JSON router — ``/api/v1/bank_accounts``.

Phase 1 tier-4 bank-accounts endpoint.

Design (a): bank accounts are a view over the ``accounts`` table — rows
where ``bsb IS NOT NULL``.  No new table is needed.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    BankAccountConflictBody,
    BankAccountCreate,
    BankAccountListOut,
    BankAccountOut,
    BankAccountUpdate,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.services import bank_accounts as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/bank_accounts",
    tags=["bank_accounts"],
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


def _dump(account: Any) -> dict[str, Any]:
    return json.loads(BankAccountOut.model_validate(account).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BankAccountListOut)
async def list_bank_accounts(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankAccountListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        limit=page_size,
        offset=offset,
    )
    return BankAccountListOut(
        items=[BankAccountOut.model_validate(a) for a in items],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{bank_account_id}", response_model=BankAccountOut)
async def get_bank_account(
    request: Request,
    bank_account_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> BankAccountOut:
    tenant_id = resolve_tenant_id(request)
    account = await svc.api_get(session, bank_account_id, tenant_id=tenant_id)
    if account is None:
        raise HTTPException(404, "Bank account not found")
    return BankAccountOut.model_validate(account)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BankAccountOut, status_code=201)
async def create_bank_account(
    request: Request,
    payload: BankAccountCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
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
        account = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            code=payload.code,
            name=payload.name,
            account_kind=payload.account_kind,
            bsb=payload.bsb,
            bank_account_number=payload.bank_account_number,
            bank_account_title=payload.bank_account_title,
            apca_user_id=payload.apca_user_id,
            bank_abbreviation=payload.bank_abbreviation,
            is_trust_account=payload.is_trust_account,
        )
    except (ValueError, svc.BankAccountError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(account)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{bank_account_id}",
    responses={
        200: {"model": BankAccountOut},
        409: {"model": BankAccountConflictBody, "description": "Version mismatch"},
    },
)
async def update_bank_account(
    request: Request,
    bank_account_id: UUID,
    payload: BankAccountUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bank account version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify bank account belongs to this tenant
    if await svc.api_get(session, bank_account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Bank account not found")

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
        account = await svc.api_update(
            session,
            bank_account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        body = BankAccountConflictBody(
            detail="version mismatch",
            current=BankAccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankAccountError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(account)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{bank_account_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": BankAccountConflictBody, "description": "Version mismatch"},
    },
)
async def delete_bank_account(
    request: Request,
    bank_account_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.api_get(session, bank_account_id, tenant_id=tenant_id)
        if existing is None:
            raise HTTPException(404, "Bank account not found")
        await hard_delete_with_audit(
            session, existing, "bank_accounts", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bank account version is required")

    if await svc.api_get(session, bank_account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Bank account not found")

    try:
        await svc.api_delete(
            session,
            bank_account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = BankAccountConflictBody(
            detail="version mismatch",
            current=BankAccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankAccountError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)
