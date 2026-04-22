"""Pure JSON accounts router — ``/api/v1/accounts``.

Phase 1 tier-1 entity. Follows the Phase 0 contacts pattern:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>``.
* Every write appends a row to ``change_log`` (handled by the service layer).
* Jinja ``/accounts`` routes remain untouched — same service layer.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.schemas import (
    AccountConflictBody,
    AccountCreate,
    AccountListOut,
    AccountOut,
    AccountUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import accounts as svc

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers (duplicated from contacts — will be refactored to a shared
# helper module once more entities land)
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession) -> UUID:
    """Return the first active company — Phase 1 single-company assumption.
    Portal JWT will carry company_id in Phase 2+."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(session: AsyncSession, key: UUID) -> JSONResponse | None:
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(content=existing.response_body, status_code=existing.response_status)


async def _remember_idempotent(
    session: AsyncSession, key: UUID, body: dict[str, Any], status_code: int
) -> None:
    row = IdempotencyKey(key=key, response_body=body, response_status=status_code)
    session.add(row)
    await session.flush()


def _dump(account: Account) -> dict[str, Any]:
    return json.loads(AccountOut.model_validate(account).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountListOut)
async def list_accounts(
    account_type: AccountType | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AccountListOut:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
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
async def get_account(account_id: UUID) -> AccountOut:
    async with AsyncSessionLocal() as session:
        account = await svc.get(session, account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        return AccountOut.model_validate(account)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        company_id = await _first_company_id(session)
        try:
            account = await svc.create(
                session,
                company_id,
                actor=f"api:{bearer[:8]}…",
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
            await _remember_idempotent(session, key, body, 201)
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
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

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
                await _remember_idempotent(session, key, body, 409)
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
            await _remember_idempotent(session, key, body, 200)
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
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay
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
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        if account is None:
            raise HTTPException(404, "Account not found")
        if key is not None:
            await _remember_idempotent(session, key, {"archived": str(account.id)}, 204)
            await session.commit()
    return Response(status_code=204)
