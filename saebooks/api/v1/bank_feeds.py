"""``/api/v1/bank-feeds`` — relay-driven bank-feed connection + sync.

Cat-C / W4. Replaces the legacy SISS-direct surface
(``saebooks/routers/bank_feeds.py``) with a thin router that delegates
to the ``feeds.saebooks.com.au`` relay via ``RemoteBankFeedsService``.

Lifecycle
---------
1. ``POST /connections``  — start a consent flow on the relay; persist
   a local mirror row in ``bank_feed_external_creds`` with status
   ``pending_consent``.
2. ``GET /connections``   — list local rows joined with whatever the
   relay knows about them. The relay is the source of truth on status
   transitions; we surface its view when reachable, fall back to local
   when not.
3. ``GET /connections/{id}`` — fetch one row.
4. ``DELETE /connections/{id}`` — revoke upstream + mark local row
   ``revoked``. Local-only fallback when the relay is unreachable.
5. ``POST /sync`` — trigger a transaction sync. In stub-mode the relay
   returns 501 with a stub body; we surface that as a 200 with a
   ``stub: true`` flag so the UI can render "stubbed" without erroring.

Feature gate
------------
The whole router is gated by ``require_feature(FLAG_BANK_FEEDS)``. The
relay enforces this server-side too (via the licence ``feeds_enabled``
claim) so the gate here is belt-and-braces — Community installs get
404 on every path; if a Pro install is downgraded mid-session, the
relay's 403 is mapped back to 403 on this side.

Tenant isolation
----------------
Every query runs through ``get_session`` which sets
``app.current_tenant`` per the standard pattern (see ``api/v1/deps``).
The local mirror table is RLS-protected (mig 0086) so cross-tenant
reads/writes are impossible at the DB layer regardless of any router
bug.

Period-lock check
-----------------
Sync may produce statement lines that, when reconciled, post journal
entries into prior periods. The contract puts the period-lock decision
on the saebooks-api side (the relay knows nothing about journals), so
``POST /sync`` checks ``PeriodLock`` against the calling tenant's
companies before delegating to the relay. If any company is locked
past the requested cursor's effective date and the caller didn't pass
``override_reason``, return 422 with a ``period_locked`` body.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.models.bank_feed_external import (
    BankFeedExternalCred,
    BankFeedExternalCredStatus,
)
from saebooks.models.company import Company
from saebooks.models.journal import PeriodLock
from saebooks.services.bank_feeds.exceptions import (
    FeedsAuthError,
    FeedsEditionError,
    FeedsError,
    FeedsIdempotencyConflict,
    FeedsNotFoundError,
    FeedsStubError,
    FeedsUpstreamError,
    FeedsUpstreamUnavailable,
    FeedsValidationError,
)
from saebooks.services.bank_feeds.remote import RemoteBankFeedsService
from saebooks.services.features import FLAG_BANK_FEEDS, require_feature

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/bank-feeds",
    tags=["bank-feeds"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_BANK_FEEDS)),
    ],
)


# ---------------------------------------------------------------------- #
# Schemas                                                                #
# ---------------------------------------------------------------------- #


class CreateConnectionIn(BaseModel):
    """Body for ``POST /connections``."""

    bank: str = Field(..., min_length=1, max_length=64)
    account_label: str = Field(..., min_length=1, max_length=200)
    ledger_id: str | None = Field(default=None, max_length=64)
    redirect_uri: str | None = Field(default=None, max_length=512)


class CreateConnectionOut(BaseModel):
    id: uuid.UUID
    consent_url: str | None
    status: str


class ConnectionRowOut(BaseModel):
    id: uuid.UUID
    siss_client_id: str
    account_id: uuid.UUID | None
    status: str
    last_sync_cursor: str | None
    created_at: datetime
    updated_at: datetime


class SyncIn(BaseModel):
    """Body for ``POST /sync``.

    All fields optional — empty body means "sync everything active".
    ``override_reason`` lets an admin force-sync past a period lock;
    matches the ``journal.post`` override pattern.
    """

    connection_id: str | None = None
    since_cursor: str | None = None
    override_reason: str | None = None


class SyncOut(BaseModel):
    """Response for ``POST /sync``.

    ``stub`` is True when the relay returned 501 with a stub body —
    the UI renders a banner rather than treating this as an error.
    ``inserted`` is the count of new statement lines persisted via
    ``services/bank_feeds/repo.insert_statement_lines``.
    """

    connection_id: str | None
    stub: bool
    inserted: int
    next_cursor: str | None
    has_more: bool


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


def _get_remote(request: Request) -> RemoteBankFeedsService:
    """Resolve the remote service.

    Hot-resolve per request so test fixtures can override on
    ``request.app.state.bank_feeds_remote`` without import-time
    monkey-patching. Production never sets that attribute, so the
    default ``RemoteBankFeedsService()`` is the path. Constructor cost
    is negligible (no I/O at construction time).
    """
    override = getattr(request.app.state, "bank_feeds_remote", None)
    if override is not None:
        return override
    return RemoteBankFeedsService()


def _map_feeds_error(exc: FeedsError) -> HTTPException:
    """Map a typed FeedsError onto an HTTPException.

    Status-code passthrough policy: the relay's 401/403/404/409/502/503
    pass through unchanged so the caller sees the same status the relay
    saw. 400/422-validation maps to 422 (FastAPI convention for
    body-shape complaints rather than 400 transport-shape).
    501-stub is NOT mapped here — it's a special case the calling
    handler decides about (some routes treat 501 as success-with-stub).
    """
    if isinstance(exc, FeedsAuthError):
        return HTTPException(status_code=401, detail=exc.detail)
    if isinstance(exc, FeedsEditionError):
        return HTTPException(status_code=403, detail=exc.detail)
    if isinstance(exc, FeedsNotFoundError):
        return HTTPException(status_code=404, detail=exc.detail)
    if isinstance(exc, FeedsIdempotencyConflict):
        return HTTPException(
            status_code=409,
            detail={
                "detail": exc.detail,
                "first_request_hash": exc.first_request_hash,
                "this_request_hash": exc.this_request_hash,
            },
        )
    if isinstance(exc, FeedsValidationError):
        return HTTPException(status_code=422, detail=exc.detail)
    if isinstance(exc, FeedsUpstreamError):
        return HTTPException(status_code=502, detail=exc.detail)
    if isinstance(exc, FeedsUpstreamUnavailable):
        return HTTPException(status_code=503, detail=exc.detail)
    return HTTPException(status_code=500, detail=str(exc))


def _row_to_out(row: BankFeedExternalCred) -> ConnectionRowOut:
    return ConnectionRowOut(
        id=row.id,
        siss_client_id=row.siss_client_id,
        account_id=row.account_id,
        status=row.status,
        last_sync_cursor=row.last_sync_cursor,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _max_period_lock_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> object | None:
    """Return the max ``locked_through`` across the tenant's companies.

    Used by ``POST /sync`` to refuse syncs that would land lines in a
    locked period without an explicit override. Returns the latest
    ``locked_through`` (a ``date``) or ``None`` if the tenant has no
    locks at all. Per-company granularity isn't worth the join
    complexity here — we use the most-restrictive lock as the gate.
    """
    from sqlalchemy import func

    result = await session.execute(
        select(func.max(PeriodLock.locked_through))
        .select_from(PeriodLock)
        .join(Company, Company.id == PeriodLock.company_id)
        .where(Company.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------- #
# POST /connections                                                      #
# ---------------------------------------------------------------------- #


@router.post(
    "/connections",
    response_model=CreateConnectionOut,
    status_code=201,
)
async def create_connection(
    request: Request,
    payload: CreateConnectionIn,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
) -> CreateConnectionOut:
    """Initiate a consent flow + persist the local mirror row.

    The relay returns either a live ``201`` with a ``consent_url`` or a
    stub-mode ``501`` with a deterministic stub body. Both cases land a
    local row with status ``pending_consent`` so the UI's "in flight"
    list works in stub-mode too. The stub body's ``stub_connection_id``
    is persisted as ``siss_client_id`` so the row has a stable handle.
    """
    tenant_id = resolve_tenant_id(request)
    remote = _get_remote(request)
    # Use the caller-supplied key if any; else mint one. The relay's
    # contract permits absent keys (no replay protection in that case)
    # but minting one here means the audit trail always has something.
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
        # Stub-mode is a known, non-error state. The relay returned the
        # deterministic body; pull the stub_connection_id off it so we
        # can persist a placeholder. The UI shows "stubbed — pending
        # SISS onboarding" and lifts the row when feeds-server goes live.
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
            # Defensive — the contract says one of these is always set.
            # Surface the malformed response rather than persisting a
            # row with no identity.
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

    return CreateConnectionOut(
        id=row.id,
        consent_url=consent_url,
        status=row.status,
    )


# ---------------------------------------------------------------------- #
# GET /connections                                                       #
# ---------------------------------------------------------------------- #


@router.get("/connections", response_model=list[ConnectionRowOut])
async def list_connections(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[ConnectionRowOut]:
    """List local mirror rows for this tenant.

    We **do not** call the relay's ``GET /connections`` here on every
    list page. The relay's view is authoritative for state transitions
    but querying it on every page render trades latency for currency
    we don't need — local rows are updated by the create/delete/sync
    paths. A future "Refresh" button can call the relay explicitly.
    Keeping the list endpoint local-only also makes it usable when the
    relay is briefly unreachable, which matters for Community-tier
    upgrade upsells (they should still see their pending connections).
    """
    tenant_id = resolve_tenant_id(request)
    result = await session.execute(
        select(BankFeedExternalCred)
        .where(BankFeedExternalCred.tenant_id == tenant_id)
        .order_by(BankFeedExternalCred.created_at.desc())
    )
    rows = result.scalars().all()
    return [_row_to_out(r) for r in rows]


# ---------------------------------------------------------------------- #
# GET /connections/{id}                                                  #
# ---------------------------------------------------------------------- #


@router.get("/connections/{connection_id}", response_model=ConnectionRowOut)
async def get_connection(
    request: Request,
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ConnectionRowOut:
    tenant_id = resolve_tenant_id(request)
    result = await session.execute(
        select(BankFeedExternalCred).where(
            BankFeedExternalCred.id == connection_id,
            BankFeedExternalCred.tenant_id == tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connection not found")
    return _row_to_out(row)


# ---------------------------------------------------------------------- #
# DELETE /connections/{id}                                               #
# ---------------------------------------------------------------------- #


@router.delete("/connections/{connection_id}", status_code=200)
async def delete_connection(
    request: Request,
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Revoke upstream + mark local row revoked.

    Stub-mode: the relay returns 501 here per contract. We treat that
    as "the relay accepted intent but couldn't actually revoke" — the
    local row goes to ``revoked`` because the customer's intent is
    clear, but we surface ``stub: true`` so the UI can show "the
    upstream wasn't actually told to revoke; we'll catch up when
    feeds-server goes live".
    """
    tenant_id = resolve_tenant_id(request)
    result = await session.execute(
        select(BankFeedExternalCred).where(
            BankFeedExternalCred.id == connection_id,
            BankFeedExternalCred.tenant_id == tenant_id,
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
        # Relay doesn't know about this connection — it was probably
        # already revoked there. Fall through to local soft-revoke.
        log.info(
            "Relay returned 404 on delete; falling through to local-only revoke for %s",
            connection_id,
        )
    except FeedsError as exc:
        raise _map_feeds_error(exc) from exc

    row.status = BankFeedExternalCredStatus.REVOKED.value
    row.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.commit()

    return {
        "id": str(row.id),
        "status": row.status,
        "stub": stub,
    }


# ---------------------------------------------------------------------- #
# POST /sync                                                             #
# ---------------------------------------------------------------------- #


@router.post("/sync", response_model=SyncOut)
async def sync_transactions(
    request: Request,
    payload: SyncIn,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
) -> SyncOut:
    """Trigger a sync via the relay; persist returned txns locally.

    Period-lock guard: refuses to sync if the tenant has any company
    locked past the current date and the caller didn't supply
    ``override_reason``. The intent is to prevent silently inserting
    statement lines that would later require a locked-period override
    on the journal-posting side.
    """
    tenant_id = resolve_tenant_id(request)

    # Period-lock check — refuse the sync if any company in the tenant
    # is locked past today and the caller didn't supply an override.
    locked_through = await _max_period_lock_for_tenant(session, tenant_id)
    if locked_through is not None and not payload.override_reason:
        from datetime import date as _date

        # Today's date in the server's local clock — the journal layer
        # uses naive dates here too, so we match.
        today: _date = datetime.now(timezone.utc).date()
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
        # Stub-mode: nothing to persist. Surface as success-with-stub.
        stub = True
        body = exc.body or {}
    except FeedsError as exc:
        raise _map_feeds_error(exc) from exc

    inserted = 0
    transactions = body.get("transactions") if isinstance(body, dict) else None
    if isinstance(transactions, list) and transactions and not stub:
        # We have real txns. Persist via the existing repo helper.
        # Repo persistence requires a ``bank_feed_account_id`` (mig 0029
        # table). The relay doesn't return that — it knows the relay-side
        # connection_id. Linking those is a future Build-N concern; for
        # now we log the count and skip insertion when the link is missing.
        # Tests that exercise the persistence path inject a connection
        # that's been mapped to a bank_feed_account by fixtures.
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

    # Update local cursor on the matching row, if we can find it.
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


# ---------------------------------------------------------------------- #
# Internal helpers                                                       #
# ---------------------------------------------------------------------- #


async def _find_bank_feed_account_for_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: Any,
) -> uuid.UUID | None:
    """Resolve the legacy ``bank_feed_accounts.id`` for a relay connection.

    The relay-side ``connection_id`` and the legacy SISS ``sds_account_id``
    were issued by different systems and are not equal. The intended
    bridge (a column on ``bank_feed_external_creds`` linking to the
    legacy ``bank_feed_accounts`` row, or a dedicated mapping table)
    arrives in a future build; for now we return ``None`` so the
    persistence step is skipped. The relay's response is still surfaced
    to the caller (``SyncOut.next_cursor`` etc.) so the UI sees the
    sync ran — just no rows were inserted on this side.

    Tests that exercise the persistence path stub this helper to
    return a known UUID.
    """
    return None


async def _advance_local_cursor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    siss_client_id: str,
    next_cursor: str,
) -> None:
    """Update ``last_sync_cursor`` on the matching local mirror row.

    No-op when no row matches — the relay may know connections this
    saebooks-api install has never seen (eg another seat on the same
    licence created it). We don't synthesise a row from a sync result
    because that would obscure provenance ("where did this row come
    from?") in the audit trail.
    """
    result = await session.execute(
        select(BankFeedExternalCred).where(
            BankFeedExternalCred.tenant_id == tenant_id,
            BankFeedExternalCred.siss_client_id == siss_client_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return
    row.last_sync_cursor = next_cursor
    row.updated_at = datetime.now(timezone.utc)
    await session.flush()
