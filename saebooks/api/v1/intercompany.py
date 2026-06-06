"""JSON router — ``/api/v1/intercompany``.

The first-class Intercompany record type (Phase 1, LOCAL / same-tenant). A
reciprocal economic event between TWO companies co-resident in one tenant DB is
recorded as ONE ``IcTxn`` (the shared event) linked to two posted
``JournalEntry`` rows (one per company) via two ``IcLeg`` rows. The per-company
"Due to/from" control account comes from the pre-declared ``IcEdge`` pair, NOT
from caller free-text — see ``services/intercompany.py``. Both legs post inside
ONE transaction (single trailing commit): if either fails, neither posts (no
half-pair, no orphan).

This router wraps ``post_local_pair`` / ``reverse_local_pair`` and mirrors the
transfers router surface:

* ``POST   /intercompany``            — post a reciprocal pair (originator +
  counterparty legs) for a same-tenant event.
* ``GET    /intercompany``            — list intercompany txns for the active
  company (matches either leg).
* ``GET    /intercompany/{id}``       — single intercompany txn.
* ``POST   /intercompany/{id}/reverse`` — reverse both legs (posts the swapped
  mirrors, flips status to REVERSED).

Auth: standard Bearer (``require_bearer``). ``tenant_id`` resolves from the JWT
claims. Both ``originator_company_id`` and ``counterparty_company_id`` are
supplied in the body (an intercompany event spans two companies, so a single
``X-Company-Id`` is insufficient); both must belong to the authenticated
tenant. ``GET``/list scope to ``X-Company-Id`` (or first active company).

No migration: ``ic_txn`` / ``ic_edges`` / ``ic_legs`` already exist (migration
0154). This router is the missing REST surface over the existing service.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcEdgeTopology,
    IcInbox,
    IcInboxStatus,
    IcLeg,
    IcTxn,
)
from saebooks.services import intercompany as svc
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay import protocol as relay_protocol
from saebooks.services.ic_relay import recon as recon_svc
from saebooks.services.ic_relay import signing as relay_signing

logger = logging.getLogger("saebooks.api.v1.intercompany")

router = APIRouter(
    prefix="/intercompany",
    tags=["intercompany"],
    dependencies=[Depends(require_bearer)],
)

# Public sub-router for the inbound relay webhook — authenticates via its own
# per-edge token + Ed25519 signature (no JWT), exactly like the paperless
# webhook. Mounted alongside `router` in saebooks/api/v1/__init__.py.
public_router = APIRouter(
    prefix="/intercompany",
    tags=["intercompany"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class IntercompanyCreate(BaseModel):
    """POST /intercompany body — a reciprocal LOCAL (same-tenant) pair.

    Both companies must be in the authenticated tenant and a reciprocal
    ``IcEdge`` pair (an ORIGINATOR edge on the originator and a COUNTERPARTY
    edge on the counterparty) must already exist; the control accounts come
    from those edges. Sign convention (fixed by the service): the originator's
    control account is DEBITED (a receivable / "due from") and its contra
    credited; the counterparty's control account is CREDITED (an obligation /
    "due to") and its contra debited.
    """

    originator_company_id: UUID
    counterparty_company_id: UUID
    amount: Decimal = Field(gt=Decimal("0"))
    entry_date: date
    originator_contra_account_id: UUID
    counterparty_contra_account_id: UUID
    description: str | None = Field(default=None, max_length=500)


class IntercompanyReverse(BaseModel):
    """POST /intercompany/{id}/reverse body."""

    reversal_date: date | None = None


class IcLegOut(BaseModel):
    id: UUID
    company_id: UUID
    journal_entry_id: UUID
    side: str

    model_config = {"from_attributes": True}


class IntercompanyOut(BaseModel):
    id: UUID
    company_id: UUID
    description: str | None
    status: str
    legs: list[IcLegOut]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IntercompanyListItem(BaseModel):
    id: UUID
    company_id: UUID
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IntercompanyListOut(BaseModel):
    items: list[IntercompanyListItem]


class ReconLegOut(BaseModel):
    """One posted leg in the reconciliation view."""

    id: UUID
    company_id: UUID
    journal_entry_id: UUID
    side: str


class ReconRowOut(BaseModel):
    """One intercompany txn in the read-only reconciliation view.

    ``matched`` is True when both legs (ORIGINATOR + COUNTERPARTY) are present —
    a complete, eliminated pair. ``outbox_status`` / ``inbox_status`` carry the
    in-flight relay state when present (None for a pure LOCAL pair).
    """

    ic_txn_id: UUID
    company_id: UUID
    status: str
    description: str | None
    matched: bool
    legs: list[ReconLegOut]
    outbox_status: str | None
    inbox_status: str | None


class IntercompanyReconOut(BaseModel):
    """Read-only intercompany reconciliation/position for the active company.

    ``unmatched_count`` is the number of txns missing a leg — the operator's
    "needs attention" tally (a stuck half-pair from a relay delivery failure
    surfaces here; the engine never auto-reverses).
    """

    items: list[ReconRowOut]
    unmatched_count: int


async def _legs_for(session: AsyncSession, ic_txn_id: UUID) -> list[IcLeg]:
    rows = (
        await session.execute(
            select(IcLeg).where(IcLeg.ic_txn_id == ic_txn_id)
        )
    ).scalars().all()
    return list(rows)


async def _serialize(
    session: AsyncSession, ic_txn_id: UUID, *, tenant_id: UUID
) -> IntercompanyOut:
    """Build the response by RE-FETCHING the txn + legs fresh in this session.

    The service commits internally (``post_local_pair`` / ``reverse_local_pair``
    both end with — or, for reverse, call ``journal.reverse`` which commits per
    leg). Reading attributes off the returned ORM instance after that commit
    triggers a lazy refresh outside the async greenlet -> ``MissingGreenlet``.
    Re-querying by id keeps every attribute access on freshly-loaded rows inside
    the current request session, so serialization never lazy-loads.
    """
    txn = (
        await session.execute(
            select(IcTxn).where(
                IcTxn.id == ic_txn_id,
                IcTxn.tenant_id == tenant_id,
            )
        )
    ).scalar_one()
    legs = await _legs_for(session, txn.id)
    return IntercompanyOut(
        id=txn.id,
        company_id=txn.company_id,
        description=txn.description,
        status=str(txn.status),
        legs=[IcLegOut.model_validate(leg) for leg in legs],
        created_at=txn.created_at,
        updated_at=txn.updated_at,
    )


# ---------------------------------------------------------------------------
# POST /intercompany
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=IntercompanyOut)
async def create_intercompany(
    payload: IntercompanyCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> IntercompanyOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        ic_txn = await svc.post_local_pair(
            session,
            tenant_id=tenant_id,
            originator_company_id=payload.originator_company_id,
            counterparty_company_id=payload.counterparty_company_id,
            amount=payload.amount,
            entry_date=payload.entry_date,
            description=payload.description,
            originator_contra_account_id=payload.originator_contra_account_id,
            counterparty_contra_account_id=payload.counterparty_contra_account_id,
            posted_by=str(actor),
        )
    except svc.IntercompanyError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "intercompany_invalid", "detail": str(exc)},
        ) from exc
    return await _serialize(session, ic_txn.id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# GET /intercompany
# ---------------------------------------------------------------------------


@router.get("", response_model=IntercompanyListOut)
async def list_intercompany(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> IntercompanyListOut:
    tenant_id = resolve_tenant_id(request)
    offset = (page - 1) * page_size
    # An ic_txn is owned by the originator company, but the active company may
    # be the counterparty — so match either ownership OR a leg in this company.
    leg_txn_ids = (
        select(IcLeg.ic_txn_id)
        .where(
            IcLeg.tenant_id == tenant_id,
            IcLeg.company_id == company_id,
        )
        .scalar_subquery()
    )
    stmt = (
        select(IcTxn)
        .where(
            IcTxn.tenant_id == tenant_id,
            (IcTxn.company_id == company_id) | (IcTxn.id.in_(leg_txn_ids)),
        )
        .order_by(IcTxn.created_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return IntercompanyListOut(
        items=[IntercompanyListItem.model_validate(t) for t in rows]
    )


# ---------------------------------------------------------------------------
# GET /intercompany/reconciliation  (read-only — Phase 3d)
# ---------------------------------------------------------------------------
# Registered BEFORE GET /{ic_txn_id} so the literal "/reconciliation" path is
# not captured by the UUID path-param matcher.


@router.get("/reconciliation", response_model=IntercompanyReconOut)
async def reconcile_intercompany(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> IntercompanyReconOut:
    """Read-only intercompany position for the active company.

    Lists every IC txn the active company participates in, each with its legs, a
    ``matched`` flag, and any in-flight relay state. SELECT-only — posts nothing,
    mutates nothing. Tenant- and company-scoped (runs under the caller's own
    FORCE-RLS; no cross-tenant data path).
    """
    tenant_id = resolve_tenant_id(request)
    rows = await recon_svc.intercompany_position(
        session, tenant_id=tenant_id, company_id=company_id
    )
    items = [
        ReconRowOut(
            ic_txn_id=r.ic_txn_id,
            company_id=r.company_id,
            status=r.status,
            description=r.description,
            matched=r.matched,
            legs=[
                ReconLegOut(
                    id=leg.id,
                    company_id=leg.company_id,
                    journal_entry_id=leg.journal_entry_id,
                    side=leg.side,
                )
                for leg in r.legs
            ],
            outbox_status=r.outbox_status,
            inbox_status=r.inbox_status,
        )
        for r in rows
    ]
    unmatched = sum(1 for r in items if not r.matched)
    return IntercompanyReconOut(items=items, unmatched_count=unmatched)


# ---------------------------------------------------------------------------
# GET /intercompany/{id}
# ---------------------------------------------------------------------------


@router.get("/{ic_txn_id}", response_model=IntercompanyOut)
async def get_intercompany(
    ic_txn_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> IntercompanyOut:
    tenant_id = resolve_tenant_id(request)
    txn = (
        await session.execute(
            select(IcTxn).where(
                IcTxn.id == ic_txn_id,
                IcTxn.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if txn is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "intercompany_not_found", "detail": "Intercompany transaction not found"},
        )
    return await _serialize(session, txn.id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# POST /intercompany/{id}/reverse
# ---------------------------------------------------------------------------


@router.post("/{ic_txn_id}/reverse", response_model=IntercompanyOut)
async def reverse_intercompany(
    ic_txn_id: UUID,
    request: Request,
    payload: IntercompanyReverse | None = None,
    session: AsyncSession = Depends(get_session),
) -> IntercompanyOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    reversal_date = payload.reversal_date if payload is not None else None
    try:
        ic_txn = await svc.reverse_local_pair(
            session,
            ic_txn_id,
            tenant_id=tenant_id,
            reversal_date=reversal_date,
            posted_by=str(actor),
        )
    except svc.IntercompanyError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "intercompany_not_found", "detail": msg},
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "intercompany_not_reversible", "detail": msg},
        ) from exc
    return await _serialize(session, ic_txn.id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# POST /api/v1/intercompany/accept  (PUBLIC inbound relay webhook — Phase 3c)
# ---------------------------------------------------------------------------
# The receiver half of the cross-DB relay. Authenticated by a per-edge bearer
# token + Ed25519 signature (NO JWT) — the paperless-webhook trust posture on an
# internal-only LAN hop. Gated by SAEBOOKS_IC_REMOTE_RELAY_ENABLED (default OFF):
# with the flag off this returns 503 and posts nothing.
#
# Order of checks (all fail-closed, opaque — never leak which check failed):
#   1. flag on?                              -> 503 if off
#   2. X-Tenant-Id valid uuid?               -> 400
#   3. bind app.current_tenant (FORCE-RLS)
#   4. dst_tenant_id in body == this tenant? -> 400 (forged routing)
#   5. load COUNTERPARTY edge under RLS      -> 404 (unknown edge)
#   6. per-edge token bcrypt-verify          -> 401
#   7. Ed25519 signature verify vs edge pubkey -> 400 (tamper / wrong sender)
#   8. freshness (issued_at within window)   -> 400 (stale / replay surface)
#   9. idempotency: ic_inbox UNIQUE(ic_txn_id) -> return prior ack, post nothing
#  10. replay: ic_inbox UNIQUE(nonce)        -> reject, post nothing
#  11. post the reciprocal leg + ic_inbox in ONE local txn -> 200 POSTED


class _RelayAccept(BaseModel):
    """Inbound relay envelope: the canonical payload + its base64 signature."""

    payload: dict
    signature: str  # base64 of the detached Ed25519 signature


@public_router.post(
    "/accept",
    summary="Inbound intercompany REMOTE relay (per-edge token + Ed25519)",
)
async def accept_relay(
    body: _RelayAccept,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Accept a relayed intercompany event and post the reciprocal leg.

    Returns 200 ``{"ic_txn_id": ..., "status": "POSTED"}`` on a fresh post or on
    an idempotent re-delivery (the prior ack). 503 when the relay flag is off;
    400/401/404 on the fail-closed checks above. NEVER posts to any tenant but
    the bound (receiver) one, and only to the receiver's OWN edge-declared
    accounts — no account id from the wire is ever trusted.
    """
    # 1. Flag gate — default OFF.
    if not settings.ic_remote_relay_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "relay_disabled", "detail": "remote relay is disabled"},
        )

    # 2. Tenant routing header.
    if not x_tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "missing_tenant", "detail": "X-Tenant-Id header required"},
        )
    try:
        tenant_uuid = uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "bad_tenant", "detail": "X-Tenant-Id must be a UUID"},
        ) from None

    payload = body.payload
    # Pull + validate the typed fields we need from the (untrusted) body. Any
    # malformed field is a flat 400 — the signature still has to cover the whole
    # canonical body, so a tampered field would also fail verify below.
    try:
        uuid.UUID(str(payload["edge_id"]))  # validate the ORIGINATOR's edge id (not used to route)
        body_src = uuid.UUID(str(payload["src_tenant_id"]))
        body_dst = uuid.UUID(str(payload["dst_tenant_id"]))
        ic_txn_id = uuid.UUID(str(payload["ic_txn_id"]))
        nonce = uuid.UUID(str(payload["nonce"]))
        amount = Decimal(str(payload["amount"]))
        entry_date = date.fromisoformat(str(payload["entry_date"]))
        description = payload.get("description") or None
        issued_at = relay_protocol.parse_issued_at(str(payload["issued_at"]))
    except (KeyError, ValueError, TypeError, relay_protocol.RelayPayloadError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "bad_payload", "detail": "malformed relay payload"},
        ) from None

    # 4. Forged-routing guard: the body's dst must equal the header tenant.
    if body_dst != tenant_uuid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "routing_mismatch", "detail": "dst_tenant_id mismatch"},
        )

    # Decode the bearer token (the per-edge scoped token, "icrl_…").
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer "):].strip()

    # 3. Bind the tenant GUC and load the edge under FORCE-RLS, then verify token
    #    + signature, then post — all under the bound app-role session so RLS is
    #    enforced for every query (no cross-tenant data path). session.info keeps
    #    the GUC alive across the post's internal commit.
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_uuid)
        async with session.begin():
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{tenant_uuid}'")
            )
            # The wire edge_id is the ORIGINATOR's (lives in the partner DB). The
            # receiver resolves ITS OWN edge by (this tenant, partner=src_tenant,
            # COUNTERPARTY) — the partner can never name one of our edge ids.
            edge = (
                await session.execute(
                    select(IcEdge).where(
                        IcEdge.tenant_id == tenant_uuid,
                        IcEdge.partner_tenant_id == body_src,
                        IcEdge.direction == IcEdgeDirection.COUNTERPARTY,
                        IcEdge.topology == IcEdgeTopology.REMOTE,
                    )
                )
            ).scalar_one_or_none()

        # 5. Unknown / wrong-direction edge.
        if edge is None or edge.relay_token_hash is None or edge.relay_pubkey is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "unknown_edge", "detail": "no such relay edge"},
            )

        local_edge_id = edge.id  # the receiver's own edge id (for the service post)

        # 6. Per-edge token (belt-and-braces over the signature). Fail closed.
        if not token or not relay_keys.verify_edge_token(token, edge.relay_token_hash):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                {"code": "bad_token", "detail": "invalid relay token"},
            )

        # 7. Ed25519 signature over the EXACT canonical bytes of the body.
        try:
            signature = bytes_from_b64(body.signature)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "bad_signature", "detail": "signature verification failed"},
            ) from None
        canonical = relay_signing.canonical_payload(payload)
        if not relay_signing.verify(canonical, signature, bytes(edge.relay_pubkey)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "bad_signature", "detail": "signature verification failed"},
            )

        # 8. Freshness — bounds the replay surface before the nonce dedupe.
        if not relay_protocol.is_fresh(
            issued_at, window_seconds=settings.ic_relay_freshness_seconds
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "stale", "detail": "message outside freshness window"},
            )

        # 9. Idempotency pre-check: a re-delivery returns the prior ack, posts
        #    nothing. The UNIQUE constraints (ic_txn_id, nonce) are the last-line
        #    race guard caught below.
        async with session.begin():
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{tenant_uuid}'")
            )
            prior = (
                await session.execute(
                    select(IcInbox).where(
                        IcInbox.tenant_id == tenant_uuid,
                        IcInbox.ic_txn_id == ic_txn_id,
                    )
                )
            ).scalar_one_or_none()
        if prior is not None:
            return JSONResponse(
                {"ic_txn_id": str(ic_txn_id), "status": str(prior.status)}
            )

    # 11. Post the reciprocal leg + ic_inbox in ONE local txn (the service owns
    #     its own commit). A duplicate that races past the pre-check trips the
    #     UNIQUE constraint -> IntegrityError -> return the idempotent ack.
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_uuid)
        # Bind the GUC for the service's first BEGIN (after_begin re-applies it).
        async with session.begin():
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{tenant_uuid}'")
            )
        try:
            local_txn = await svc.accept_remote_counterparty(
                session,
                tenant_id=tenant_uuid,
                edge_id=local_edge_id,
                ic_txn_id=ic_txn_id,
                nonce=nonce,
                amount=amount,
                entry_date=entry_date,
                description=description,
                payload_json=payload,
                signature=signature,
                posted_by="ic-relay",
            )
        except IntegrityError:
            await session.rollback()
            return JSONResponse(
                {"ic_txn_id": str(ic_txn_id), "status": str(IcInboxStatus.POSTED)}
            )
        except svc.IntercompanyError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "post_failed", "detail": str(exc)},
            ) from exc

    logger.info(
        "ic-relay accepted ic_txn_id=%s for tenant=%s (local_txn=%s)",
        ic_txn_id, tenant_uuid, local_txn.id,
    )
    return JSONResponse({"ic_txn_id": str(ic_txn_id), "status": "POSTED"})


def bytes_from_b64(value: str) -> bytes:
    """Decode a base64 signature string, raising ValueError on garbage."""
    from base64 import b64decode

    return b64decode(value.encode("ascii"), validate=True)
