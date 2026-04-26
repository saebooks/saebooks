"""Pure JSON accounts router — ``/api/v1/accounts``.

Phase 1 tier-1 entity. Follows the Phase 0 contacts pattern:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>``.
* Every write appends a row to ``change_log`` (handled by the service layer).
* Jinja ``/accounts`` routes remain untouched — same service layer.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; every query is gated by the ``tenant_isolation`` RLS
policy from migration 0055. ``_first_company_id`` is scoped by the
request tenant. ``svc.get`` is called with ``tenant_id`` so a
foreign-tenant UUID returns ``None`` (404) even if the row exists.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    AccountConflictBody,
    AccountCreate,
    AccountListOut,
    AccountOut,
    AccountUpdate,
)
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services import accounts as svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
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
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(account: Account) -> dict[str, Any]:
    return json.loads(AccountOut.model_validate(account).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountListOut)
async def list_accounts(
    request: Request,
    account_type: AccountType | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AccountListOut:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    count_stmt = (
        select(func.count())
        .select_from(Account)
        .where(Account.company_id == company_id, Account.archived_at.is_(None))
    )
    if account_type is not None:
        count_stmt = count_stmt.where(Account.account_type == account_type)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Account)
        .where(Account.company_id == company_id, Account.archived_at.is_(None))
        .order_by(Account.code)
        .offset(offset)
        .limit(limit)
    )
    if account_type is not None:
        stmt = stmt.where(Account.account_type == account_type)
    items = list((await session.execute(stmt)).scalars().all())
    return AccountListOut(
        items=[AccountOut.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountOut:
    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    return AccountOut.model_validate(account)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    company_id = await _first_company_id(session, tenant_id)
    try:
        account = await svc.create(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
            code=payload.code,
            name=payload.name,
            account_type=payload.account_type,
            reconcile=payload.reconcile,
            is_header=payload.is_header,
            tax_code_default=payload.tax_code_default,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    await session.refresh(account)
    body = _dump(account)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{account_id}",
    responses={
        200: {"model": AccountOut},
        409: {"model": AccountConflictBody, "description": "Version mismatch"},
    },
)
async def update_account(
    account_id: UUID,
    payload: AccountUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
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
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    if await svc.get(session, account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Account not found")

    try:
        account = await svc.update(
            session,
            account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = AccountConflictBody(
            detail="version mismatch",
            current=AccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(account)
    body = _dump(account)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive via archived_at)
# ---------------------------------------------------------------------------


@router.delete(
    "/{account_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": AccountConflictBody, "description": "Version mismatch"},
    },
)
async def archive_account(
    account_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
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
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 204,
            )

    if await svc.get(session, account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Account not found")

    try:
        account = await svc.archive(
            session,
            account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = AccountConflictBody(
            detail="version mismatch",
            current=AccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    if account is None:
        raise HTTPException(404, "Account not found")
    if key is not None:
        archived_body = json.dumps({"archived": str(account.id)}).encode()
        await store_response(session, key, 204, archived_body)
        await session.commit()
    return Response(status_code=204)
