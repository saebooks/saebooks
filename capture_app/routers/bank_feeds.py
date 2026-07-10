"""Module routes for the bank-feeds REST surface — thin shell over the
engine's ``api/v1/bank_feeds`` handlers' service calls.

Re-implements the connection lifecycle (create / list / get / delete) and the
transaction ``sync`` against the shared DB with RLS bound to the
``X-Tenant-Id`` header. The engine router's helpers (``_get_remote`` /
``_map_feeds_error`` / ``_row_to_out`` / ``_max_period_lock_for_tenant`` /
``_find_bank_feed_account_for_connection`` / ``_advance_local_cursor``) and
its request/response schemas are imported and reused verbatim — this router
only swaps JWT-derived tenant resolution for the header-derived context and
gates on ``X-Capture-Token`` instead of ``require_bearer`` +
``require_feature`` (the engine already applied the feature gate before
delegating).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from capture_app.deps import (
    TenantContext,
    get_module_session,
    get_tenant_context,
    require_capture_token,
)
from saebooks.api.v1.bank_feeds import (
    ConnectionRowOut,
    CreateConnectionIn,
    CreateConnectionOut,
    SyncIn,
    SyncOut,
    _advance_local_cursor,
    _find_bank_feed_account_for_connection,
    _get_remote,
    _map_feeds_error,
    _max_period_lock_for_tenant,
    _row_to_out,
)
from saebooks.models.bank_feed_external import (
    BankFeedExternalCred,
    BankFeedExternalCredStatus,
)
from saebooks.services.bank_feeds.exceptions import (
    FeedsError,
    FeedsNotFoundError,
    FeedsStubError,
)

router = APIRouter(
    prefix="/bank-feeds",
    tags=["capture-bank-feeds"],
    dependencies=[Depends(require_capture_token)],
)


@router.post("/connections", response_model=CreateConnectionOut, status_code=201)
async def create_connection(
    request: Request,
    payload: CreateConnectionIn,
    ctx: TenantContext = Depends(get_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_module_session),
) -> CreateConnectionOut:
    """Initiate a consent flow + persist the local mirror row."""
    tenant_id = ctx.tenant_id
    remote = _get_remote(request)
    key = idempotency_key or str(uuid.uuid4())

    consent_url: str | None = None
    siss_client_id: str
    try:
        body = await remote.create_connection(
            bank=payload.bank,
            account_label=payload.account_label,
            idempotency_key=key,
            ledger_id=payload.ledger_id,
            redirect_uri=payload.redirect_uri,
        )
    except FeedsStubError as exc:
        stub_body = exc.body or {}
        siss_client_id = str(
            stub_body.get("stub_connection_id") or f"stub_conn_{uuid.uuid4()}"
        )
        consent_url = stub_body.get("stub_consent_url")
    except FeedsError as exc:
        raise _map_feeds_error(exc) from exc
    else:
        siss_client_id = str(
            body.get("connection_id") or body.get("stub_connection_id") or ""
        )
        if not siss_client_id:
            raise HTTPException(
                status_code=502,
                detail="Feeds-server response missing connection_id",
            )
        consent_url = body.get("consent_url")

    row = BankFeedExternalCred(
        tenant_id=tenant_id,
        siss_client_id=siss_client_id,
        status=BankFeedExternalCredStatus.PENDING_CONSENT.value,
    )
    session.add(row)
    await session.flush()
    await session.commit()

    return CreateConnectionOut(id=row.id, consent_url=consent_url, status=row.status)


@router.get("/connections", response_model=list[ConnectionRowOut])
async def list_connections(
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
    _tok: None = Depends(require_capture_token),
) -> list[ConnectionRowOut]:
    """List local mirror rows for this tenant."""
    result = await session.execute(
        select(BankFeedExternalCred)
        .where(BankFeedExternalCred.tenant_id == ctx.tenant_id)
        .order_by(BankFeedExternalCred.created_at.desc())
    )
    return [_row_to_out(r) for r in result.scalars().all()]


@router.get("/connections/{connection_id}", response_model=ConnectionRowOut)
async def get_connection(
    connection_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
    _tok: None = Depends(require_capture_token),
) -> ConnectionRowOut:
    result = await session.execute(
        select(BankFeedExternalCred).where(
            BankFeedExternalCred.id == connection_id,
            BankFeedExternalCred.tenant_id == ctx.tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connection not found")
    return _row_to_out(row)


@router.delete("/connections/{connection_id}", status_code=200)
async def delete_connection(
    request: Request,
    connection_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> dict[str, Any]:
    """Revoke upstream + mark local row revoked."""
    result = await session.execute(
        select(BankFeedExternalCred).where(
            BankFeedExternalCred.id == connection_id,
            BankFeedExternalCred.tenant_id == ctx.tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connection not found")

    remote = _get_remote(request)
    stub = False
    try:
        await remote.delete_connection(row.siss_client_id)
    except FeedsStubError:
        stub = True
    except FeedsNotFoundError:
        pass
    except FeedsError as exc:
        raise _map_feeds_error(exc) from exc

    row.status = BankFeedExternalCredStatus.REVOKED.value
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await session.commit()

    return {"id": str(row.id), "status": row.status, "stub": stub}


@router.post("/sync", response_model=SyncOut)
async def sync_transactions(
    request: Request,
    payload: SyncIn,
    ctx: TenantContext = Depends(get_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_module_session),
) -> SyncOut:
    """Trigger a sync via the relay; persist returned txns locally."""
    tenant_id = ctx.tenant_id

    locked_through = await _max_period_lock_for_tenant(session, tenant_id)
    if locked_through is not None and not payload.override_reason:
        today = datetime.now(UTC).date()
        if today <= locked_through:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "period_locked",
                    "locked_through": str(locked_through),
                    "message": (
                        "Tenant has a period locked through "
                        f"{locked_through}; supply override_reason to sync."
                    ),
                },
            )

    remote = _get_remote(request)
    key = idempotency_key or str(uuid.uuid4())
    stub = False
    body: dict[str, Any] = {}
    try:
        body = await remote.sync_transactions(
            connection_id=payload.connection_id,
            since_cursor=payload.since_cursor,
            idempotency_key=key,
        )
    except FeedsStubError as exc:
        stub = True
        body = exc.body or {}
    except FeedsError as exc:
        raise _map_feeds_error(exc) from exc

    inserted = 0
    transactions = body.get("transactions") if isinstance(body, dict) else None
    if isinstance(transactions, list) and transactions and not stub:
        link = await _find_bank_feed_account_for_connection(
            session,
            tenant_id=tenant_id,
            connection_id=body.get("connection_id"),
        )
        if link is not None:
            from saebooks.services.bank_feeds import repo as bf_repo

            inserted = await bf_repo.insert_statement_lines(
                session,
                bank_feed_account_id=link,
                transactions=transactions,
            )

    target_id = body.get("connection_id") if isinstance(body, dict) else None
    if target_id and not stub:
        next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
        if next_cursor:
            await _advance_local_cursor(
                session,
                tenant_id=tenant_id,
                siss_client_id=str(target_id),
                next_cursor=str(next_cursor),
            )

    await session.commit()

    return SyncOut(
        connection_id=(
            str(body.get("connection_id"))
            if isinstance(body, dict) and body.get("connection_id")
            else None
        ),
        stub=stub,
        inserted=inserted,
        next_cursor=(
            str(body.get("next_cursor"))
            if isinstance(body, dict) and body.get("next_cursor")
            else None
        ),
        has_more=bool(body.get("has_more")) if isinstance(body, dict) else False,
    )
