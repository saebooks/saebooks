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

import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    BankStatementLineConflictBody,
    BankStatementLineCreate,
    BankStatementLineListOut,
    BankStatementLineOut,
    BankStatementLineUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.bank_statement import StatementLineStatus
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import bank_statement_lines as svc

router = APIRouter(
    prefix="/bank_statement_lines",
    tags=["bank_statement_lines"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session) -> UUID:
    """Return the first active company — Phase 1 single-company assumption."""
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
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(session, key: UUID) -> JSONResponse | None:
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(content=existing.response_body, status_code=existing.response_status)


async def _remember_idempotent(
    session, key: UUID, body: dict[str, Any], status_code: int
) -> None:
    row = IdempotencyKey(key=key, response_body=body, response_status=status_code)
    session.add(row)
    await session.flush()


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


@router.get("", response_model=BankStatementLineListOut)
async def list_bank_statement_lines(
    bank_account_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> BankStatementLineListOut:
    status_filter = _parse_status(status)
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        items, total = await svc.list_active(
            session,
            company_id,
            tenant_id,
            account_id=bank_account_id,
            status=status_filter,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return BankStatementLineListOut(
            items=[BankStatementLineOut.model_validate(l) for l in items],
            total=total,
            limit=limit,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{line_id}", response_model=BankStatementLineOut)
async def get_bank_statement_line(line_id: UUID) -> BankStatementLineOut:
    async with AsyncSessionLocal() as session:
        line = await svc.api_get(session, line_id)
        if line is None or line.archived_at is not None:
            raise HTTPException(404, "Bank statement line not found")
        return BankStatementLineOut.model_validate(line)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BankStatementLineOut, status_code=201)
async def create_bank_statement_line(
    payload: BankStatementLineCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)

    # Validate status value in payload
    status_val = _parse_status(payload.status) or StatementLineStatus.UNMATCHED

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
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
            await _remember_idempotent(session, key, body, 201)
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
    line_id: UUID,
    payload: BankStatementLineUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with line version is required")
    key = _parse_idempotency_key(idempotency_key)

    # Convert status string to enum if present
    update_kwargs = payload.model_dump(exclude_unset=True)
    if "status" in update_kwargs and update_kwargs["status"] is not None:
        update_kwargs["status"] = _parse_status(update_kwargs["status"])

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

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
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.BankStatementLineError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(line)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
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
    line_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with line version is required")

    async with AsyncSessionLocal() as session:
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
