"""``/api/v1/sync/xero`` — operator-facing Xero sync surface.

Endpoints
---------
* ``POST   /api/v1/sync/xero/connect``    — start consent flow
* ``GET    /api/v1/sync/xero/callback``   — OAuth redirect target
* ``DELETE /api/v1/sync/xero/{id}``       — disconnect / revoke
* ``GET    /api/v1/sync/xero/status``     — list connections + last
                                            sync summary
* ``POST   /api/v1/sync/xero/{id}/trigger`` — kick a sync run

Feature gate
------------
The whole router is gated by both ``FLAG_ACCOUNTING_SYNC`` (parent flag
that surfaces the "Connections" tab in the UI) and ``FLAG_SYNC_XERO``
(per-provider switch). Either being False returns 404 — both are
Enterprise-tier.

Consent flow (PKCE)
-------------------
``POST /connect`` does NOT itself talk to Xero. It mints a state token,
a PKCE code-verifier (stored encrypted on the connection row), and
returns the constructed authorize URL. The operator's browser handles
the redirect.

``GET /callback`` validates state + verifier, exchanges the code for
tokens, and fetches the available orgs via ``GET /connections``. If
there's exactly one org, we save its tenant_id and mark the connection
``active``. If there are several, we set the connection to
``pending_consent`` with ``external_tenant_id IS NULL`` (the schema
allows it) and the operator picks one in a follow-up call.

In this initial cut we ship the single-org path only — multi-org
selection lives in a follow-up.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.models.sync import (
    SyncConnection,
    SyncConnectionStatus,
    SyncDirection,
    SyncProvider,
)
from saebooks.services.crypto import encrypt_field
from saebooks.services.features import (
    FLAG_ACCOUNTING_SYNC,
    FLAG_SYNC_XERO,
    require_feature,
)
from saebooks.services.sync.errors import SyncError
from saebooks.services.sync.xero.connector import sync_xero
from saebooks.services.sync.xero.token import (
    build_authorize_url,
    exchange_code_for_tokens,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sync/xero",
    tags=["sync-xero"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_ACCOUNTING_SYNC)),
        Depends(require_feature(FLAG_SYNC_XERO)),
    ],
)


# Xero OAuth scopes we request. Per plan §11.b: read+write on contacts,
# invoices, accounting transactions, and accounts (read-only on CoA).
# ``offline_access`` is mandatory for refresh-token issuance.
_DEFAULT_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.transactions",
    "accounting.contacts",
    "accounting.settings.read",
    "accounting.journals.read",
]


# Cache PKCE verifiers by ``state`` -> (verifier, redirect_uri,
# tenant_id). Process-local; the consent loop completes within a
# single API process so this is sufficient for v1. Multi-process
# deployments would need Redis here.
_CONSENT_CACHE: dict[str, tuple[str, str, uuid.UUID]] = {}


# ---------------------------------------------------------------------- #
# Schemas                                                                #
# ---------------------------------------------------------------------- #


class ConnectIn(BaseModel):
    """Body for ``POST /connect``."""

    client_id: str = Field(..., min_length=1, max_length=200)
    client_secret: str = Field(..., min_length=1, max_length=200)
    redirect_uri: str = Field(..., min_length=1, max_length=512)


class ConnectOut(BaseModel):
    """Response shape: caller redirects the browser to ``authorize_url``."""

    connection_id: uuid.UUID
    authorize_url: str
    state: str


class ConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    external_tenant_id: str | None
    external_tenant_name: str | None
    status: str
    last_pulled_at: datetime | None
    last_pushed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class TriggerOut(BaseModel):
    connection_id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime
    summary: dict[str, Any]


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256-challenge). 43-char URL-safe verifier."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _row_to_out(row: SyncConnection) -> ConnectionOut:
    return ConnectionOut(
        id=row.id,
        provider=row.provider,
        external_tenant_id=row.external_tenant_id,
        external_tenant_name=row.external_tenant_name,
        status=row.status,
        last_pulled_at=row.last_pulled_at,
        last_pushed_at=row.last_pushed_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _list_xero_connections(
    *,
    access_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Call ``GET https://api.xero.com/connections`` and return the list.

    Lives here (not in ``endpoints.py``) because at this point we have
    not chosen a tenant yet — ``XeroClient`` requires a tenant to
    construct.
    """
    own = http_client is None
    http = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await http.get(
            "https://api.xero.com/connections",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Xero connections call failed ({resp.status_code}): {resp.text[:200]}",
            )
        body = resp.json()
        if not isinstance(body, list):
            raise HTTPException(
                status_code=502,
                detail="Xero connections response was not a list",
            )
        return body
    finally:
        if own:
            await http.aclose()


# ---------------------------------------------------------------------- #
# POST /connect                                                          #
# ---------------------------------------------------------------------- #


@router.post(
    "/connect",
    response_model=ConnectOut,
    status_code=201,
)
async def connect(
    request: Request,
    payload: ConnectIn,
    session: AsyncSession = Depends(get_session),
) -> ConnectOut:
    """Start a Xero consent flow.

    Persists the customer's OAuth ``client_id``/``client_secret`` (Fernet
    -encrypted) on a new ``sync_connections`` row in
    ``pending_consent`` status. Returns the URL the operator's browser
    should redirect to.
    """
    tenant_id = resolve_tenant_id(request)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()

    row = SyncConnection(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider=SyncProvider.XERO.value,
        external_tenant_id=None,
        oauth_client_id_ciphertext=encrypt_field(payload.client_id).encode("ascii"),
        oauth_client_secret_ciphertext=encrypt_field(payload.client_secret).encode(
            "ascii"
        ),
        oauth_scopes=" ".join(_DEFAULT_SCOPES),
        redirect_uri=payload.redirect_uri,
        status=SyncConnectionStatus.PENDING_CONSENT.value,
    )
    session.add(row)
    await session.flush()

    _CONSENT_CACHE[state] = (verifier, payload.redirect_uri, row.id)

    authorize_url = build_authorize_url(
        client_id=payload.client_id,
        redirect_uri=payload.redirect_uri,
        scopes=_DEFAULT_SCOPES,
        state=state,
        code_challenge=challenge,
    )

    await session.commit()

    return ConnectOut(
        connection_id=row.id,
        authorize_url=authorize_url,
        state=state,
    )


# ---------------------------------------------------------------------- #
# GET /callback                                                          #
# ---------------------------------------------------------------------- #


@router.get("/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """OAuth redirect target.

    Xero redirects the operator's browser here with ``?code=...&state=...``.
    We exchange the code for tokens, fetch the available orgs, and
    transition the connection to ``active``.

    The endpoint deliberately requires the bearer + feature flag — i.e.
    the *operator session* must still be alive in the API. If the
    operator has logged out, the redirect lands on a 401 and the
    connection stays ``pending_consent``.
    """
    tenant_id = resolve_tenant_id(request)
    cached = _CONSENT_CACHE.pop(state, None)
    if cached is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown or expired state — restart the consent flow",
        )
    verifier, redirect_uri, connection_id = cached

    stmt = select(SyncConnection).where(
        SyncConnection.id == connection_id,
        SyncConnection.tenant_id == tenant_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    if row.oauth_client_id_ciphertext is None or row.oauth_client_secret_ciphertext is None:
        raise HTTPException(
            status_code=400,
            detail="Connection missing OAuth client credentials",
        )

    from saebooks.services.crypto import decrypt_field

    client_id = decrypt_field(row.oauth_client_id_ciphertext.decode("ascii"))
    client_secret = decrypt_field(row.oauth_client_secret_ciphertext.decode("ascii"))

    body = await exchange_code_for_tokens(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise HTTPException(
            status_code=502,
            detail="Xero token response missing access/refresh token",
        )

    # Fetch available Xero orgs.
    orgs = await _list_xero_connections(access_token=access_token)
    if not orgs:
        raise HTTPException(
            status_code=400,
            detail="No Xero organisations available — grant access on at least one org",
        )

    # Single-org path only for v1.
    org = orgs[0]
    row.external_tenant_id = org.get("tenantId")
    row.external_tenant_name = org.get("tenantName")
    row.oauth_refresh_token_ciphertext = encrypt_field(refresh_token).encode("ascii")
    row.status = SyncConnectionStatus.ACTIVE.value
    row.last_error = None

    # Audit log: connect event.
    from saebooks.models.sync import SyncAuditLog

    session.add(
        SyncAuditLog(
            tenant_id=tenant_id,
            connection_id=row.id,
            direction=SyncDirection.CONNECT.value,
            outcome="ok",
            message=f"Connected to Xero org {row.external_tenant_name!r}",
            payload={"orgs_offered": len(orgs)},
        )
    )

    await session.commit()
    return {
        "connection_id": str(row.id),
        "external_tenant_id": row.external_tenant_id,
        "external_tenant_name": row.external_tenant_name,
        "status": row.status,
    }


# ---------------------------------------------------------------------- #
# GET /status                                                            #
# ---------------------------------------------------------------------- #


@router.get("/status", response_model=list[ConnectionOut])
async def status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[ConnectionOut]:
    """List all Xero connections for the current tenant."""
    tenant_id = resolve_tenant_id(request)
    stmt = select(SyncConnection).where(
        SyncConnection.tenant_id == tenant_id,
        SyncConnection.provider == SyncProvider.XERO.value,
    )
    rows = list((await session.execute(stmt)).scalars())
    return [_row_to_out(r) for r in rows]


# ---------------------------------------------------------------------- #
# DELETE /{id}                                                           #
# ---------------------------------------------------------------------- #


@router.delete("/{connection_id}", status_code=204)
async def disconnect(
    request: Request,
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke a Xero connection.

    Marks the row ``revoked`` and zeroes the refresh-token ciphertext.
    Local sync_state rows are kept (they're useful for re-linking on
    re-connect) but the connection's ``oauth_refresh_token_ciphertext``
    is wiped so the credential cannot be reused.
    """
    tenant_id = resolve_tenant_id(request)
    stmt = select(SyncConnection).where(
        SyncConnection.id == connection_id,
        SyncConnection.tenant_id == tenant_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    row.status = SyncConnectionStatus.REVOKED.value
    row.oauth_refresh_token_ciphertext = None
    row.last_error = None

    from saebooks.models.sync import SyncAuditLog

    session.add(
        SyncAuditLog(
            tenant_id=tenant_id,
            connection_id=row.id,
            direction=SyncDirection.DISCONNECT.value,
            outcome="ok",
            message="Operator-initiated disconnect",
        )
    )
    await session.commit()


# ---------------------------------------------------------------------- #
# POST /{id}/trigger                                                     #
# ---------------------------------------------------------------------- #


@router.post("/{connection_id}/trigger", response_model=TriggerOut)
async def trigger(
    request: Request,
    connection_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> TriggerOut:
    """Run one full sync cycle synchronously.

    Returns the run report. For long syncs the worker handles the
    cadence; this endpoint is for operator-initiated kicks.
    """
    tenant_id = resolve_tenant_id(request)
    stmt = select(SyncConnection).where(
        SyncConnection.id == connection_id,
        SyncConnection.tenant_id == tenant_id,
    ).with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    if row.status not in {
        SyncConnectionStatus.ACTIVE.value,
        SyncConnectionStatus.ERROR.value,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Connection status is {row.status!r}; cannot trigger",
        )

    try:
        report = await sync_xero(session, connection=row)
    except SyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await session.commit()

    return TriggerOut(
        connection_id=row.id,
        status=report.status,
        started_at=report.started_at,
        finished_at=report.finished_at,
        summary=report.to_dict(),
    )


__all__ = ["router"]
