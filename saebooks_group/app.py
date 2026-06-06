"""saebooks-group broker FastAPI app — money-free relay (Phases 3b + 3c).

Endpoints
---------
* ``POST /ic/pairs``      — register a pair (public keys + token hashes). Called
                            by the authoriser edge-enable flow.
* ``GET  /ic/pairs``      — list registered pairs (no secrets in the response).
* ``GET  /ic/relay-log``  — read-only delivery log.
* ``POST /ic/relay``      — inbound from an originator dispatcher. In 3b this is
                            **501** (registered but inert). In 3c (when
                            ``SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED`` is on) it
                            verifies the originator signature against the pair's
                            ``src_pubkey``, replay-guards on ``(edge_id, nonce)``,
                            and forwards the SAME signed envelope to the
                            partner's ``dst_endpoint`` ``/ic/accept``.
* ``GET  /healthz``       — liveness.

The broker holds NO money: no GL tables, and it imports NO posting code. It
reuses only the crypto-pure ``saebooks.services.ic_relay.signing`` for verify.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from base64 import b64decode
from datetime import UTC, datetime

import bcrypt
import httpx
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from saebooks.services.ic_relay import protocol as relay_protocol
from saebooks.services.ic_relay import signing as relay_signing
from saebooks_group.config import settings
from saebooks_group.db import SessionLocal
from saebooks_group.models import (
    PairRegistry,
    PairStatus,
    RelayDirection,
    RelayLog,
    RelayStatus,
)

logger = logging.getLogger("saebooks_group.app")

app = FastAPI(title="SAE Books — Intercompany Relay Broker", version="0.1.0")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class PairRegister(BaseModel):
    edge_id: uuid.UUID
    src_tenant_id: uuid.UUID
    dst_tenant_id: uuid.UUID
    src_endpoint: str
    dst_endpoint: str
    src_pubkey_b64: str
    dst_pubkey_b64: str
    # The broker stores only HASHES of these; cleartext is presented to verify.
    src_relay_token: str | None = None
    dst_relay_token: str | None = None


class RelayEnvelope(BaseModel):
    payload: dict
    signature: str  # base64


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# POST /ic/pairs — register (or update) a pair
# --------------------------------------------------------------------------- #
@app.post("/ic/pairs", status_code=status.HTTP_201_CREATED)
async def register_pair(body: PairRegister) -> JSONResponse:
    """Register a REMOTE edge pair. Stores PUBLIC keys + token HASHES only.

    Idempotent on ``edge_id`` (re-register updates endpoints/keys — supports key
    rotation). Never stores a private key, a token cleartext, or any money.
    """
    try:
        src_pub = b64decode(body.src_pubkey_b64.encode("ascii"), validate=True)
        dst_pub = b64decode(body.dst_pubkey_b64.encode("ascii"), validate=True)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad pubkey encoding") from None

    src_hash = _hash_token(body.src_relay_token)
    dst_hash = _hash_token(body.dst_relay_token)

    async with SessionLocal() as session, session.begin():
        existing = (
            await session.execute(
                select(PairRegistry).where(PairRegistry.edge_id == body.edge_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PairRegistry(
                    edge_id=body.edge_id,
                    src_tenant_id=body.src_tenant_id,
                    dst_tenant_id=body.dst_tenant_id,
                    src_endpoint=body.src_endpoint,
                    dst_endpoint=body.dst_endpoint,
                    src_pubkey=src_pub,
                    dst_pubkey=dst_pub,
                    src_relay_token_hash=src_hash,
                    dst_relay_token_hash=dst_hash,
                    status=PairStatus.ACTIVE,
                )
            )
        else:
            existing.src_tenant_id = body.src_tenant_id
            existing.dst_tenant_id = body.dst_tenant_id
            existing.src_endpoint = body.src_endpoint
            existing.dst_endpoint = body.dst_endpoint
            existing.src_pubkey = src_pub
            existing.dst_pubkey = dst_pub
            if src_hash is not None:
                existing.src_relay_token_hash = src_hash
            if dst_hash is not None:
                existing.dst_relay_token_hash = dst_hash
            existing.status = PairStatus.ACTIVE

    return JSONResponse(
        {"edge_id": str(body.edge_id), "status": "ACTIVE"},
        status_code=status.HTTP_201_CREATED,
    )


@app.get("/ic/pairs")
async def list_pairs() -> dict[str, list[dict]]:
    """List registered pairs. NEVER returns keys or token hashes."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(PairRegistry))).scalars().all()
    return {
        "pairs": [
            {
                "edge_id": str(r.edge_id),
                "src_tenant_id": str(r.src_tenant_id),
                "dst_tenant_id": str(r.dst_tenant_id),
                "dst_endpoint": r.dst_endpoint,
                "status": str(r.status),
            }
            for r in rows
        ]
    }


@app.get("/ic/relay-log")
async def relay_log(limit: int = 100) -> dict[str, list[dict]]:
    """Read-only delivery log (routing + audit only — no money fields)."""
    limit = max(1, min(limit, 500))
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(RelayLog).order_by(RelayLog.received_at.desc()).limit(limit)
            )
        ).scalars().all()
    return {
        "entries": [
            {
                "id": str(r.id),
                "ic_txn_id": str(r.ic_txn_id),
                "edge_id": str(r.edge_id),
                "direction": str(r.direction),
                "status": str(r.status),
                "attempts": r.attempts,
                "sig_fingerprint": r.sig_fingerprint,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
                "last_error": r.last_error,
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------------- #
# POST /ic/relay — inbound from an originator dispatcher
# --------------------------------------------------------------------------- #
@app.post("/ic/relay")
async def relay(
    body: RelayEnvelope,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Verify the originator signature + replay-guard, then forward to partner.

    Phase 3b: returns **501** (forwarding disabled) — the stack stands up and the
    pair/log endpoints work, but no message is forwarded yet.

    Phase 3c (``SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED`` on): looks up the pair
    by ``edge_id``, verifies the per-edge token + the Ed25519 signature against
    ``src_pubkey``, dedupes on ``(edge_id, nonce)``, records the relay_log row,
    and forwards the SAME signed envelope to ``dst_endpoint`` ``/ic/accept`` with
    the dst per-edge token.
    """
    if not settings.relay_forwarding_enabled:
        # Phase 3b — inert. Stand the stack up; do not forward.
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "relay forwarding not enabled (phase 3b: broker is read-only)",
        )

    payload = body.payload
    try:
        edge_id = uuid.UUID(str(payload["edge_id"]))
        nonce = uuid.UUID(str(payload["nonce"]))
        ic_txn_id = uuid.UUID(str(payload["ic_txn_id"]))
        issued_at = relay_protocol.parse_issued_at(str(payload["issued_at"]))
    except (KeyError, ValueError, TypeError, relay_protocol.RelayPayloadError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed payload") from None

    # Freshness — reject a stale / far-future envelope at the FIRST hop, before
    # the (edge_id, nonce) dedupe, so a captured message cannot be re-injected
    # through the broker outside a tight window (mirrors /ic/accept).
    if not relay_protocol.is_fresh(
        issued_at, window_seconds=settings.relay_freshness_seconds
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "message outside freshness window"
        )

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer "):].strip()

    async with SessionLocal() as session, session.begin():
        pair = (
            await session.execute(
                select(PairRegistry).where(PairRegistry.edge_id == edge_id)
            )
        ).scalar_one_or_none()
        if pair is None or pair.status != PairStatus.ACTIVE:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or inactive pair")

        # Per-edge token (the SRC side presents its token to the broker).
        if pair.src_relay_token_hash is None or not _verify_token(
            token, pair.src_relay_token_hash
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

        # Verify the originator signature over the EXACT canonical bytes.
        try:
            sig = b64decode(body.signature.encode("ascii"), validate=True)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad signature") from None
        canonical = relay_signing.canonical_payload(payload)
        if not relay_signing.verify(canonical, sig, bytes(pair.src_pubkey)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "signature verify failed")

        # Replay guard: the broker also dedupes on (edge_id, nonce).
        dup = (
            await session.execute(
                select(RelayLog).where(
                    RelayLog.edge_id == edge_id, RelayLog.nonce == nonce
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            # REJECTED-REPLAY: the broker has already seen this (edge_id, nonce).
            # It must NOT look like a genuine first delivery — flag it so the
            # originator dispatcher does NOT mark the outbox row delivered/ACKED
            # off a replay (the false-positive-ack defect). Nothing is forwarded.
            return JSONResponse(
                {
                    "ic_txn_id": str(ic_txn_id),
                    "status": str(dup.status),
                    "duplicate": True,
                    "delivered": False,
                }
            )

        log_row = RelayLog(
            ic_txn_id=ic_txn_id,
            edge_id=edge_id,
            nonce=nonce,
            direction=RelayDirection.SRC_TO_DST,
            sig_fingerprint=hashlib.sha256(sig).hexdigest(),
            status=RelayStatus.RECEIVED,
            payload_json=payload,
        )
        session.add(log_row)
        dst_endpoint = pair.dst_endpoint
        dst_tenant_id = pair.dst_tenant_id
        dst_token_present = pair.dst_relay_token_hash is not None
        log_id = log_row.id

    # Forward OUTSIDE the broker txn. The broker presents the DST per-edge token;
    # we don't have its cleartext (we store only the hash) — in production the
    # cleartext lives in the broker's secret store keyed by edge. The forward
    # carries the same envelope + X-Tenant-Id so the receiver binds the right
    # tenant. (Token cleartext sourcing is a go-live secret-wiring step.)
    forwarded_ok = False
    last_error: str | None = None
    dst_token = _resolve_dst_token(edge_id) if dst_token_present else None
    try:
        async with httpx.AsyncClient(timeout=settings.forward_timeout_seconds) as client:
            headers = {"X-Tenant-Id": str(dst_tenant_id)}
            if dst_token:
                headers["Authorization"] = f"Bearer {dst_token}"
            resp = await client.post(
                f"{dst_endpoint.rstrip('/')}/ic/accept",
                json={"payload": payload, "signature": body.signature},
                headers=headers,
            )
        forwarded_ok = resp.status_code // 100 == 2
        if not forwarded_ok:
            last_error = f"partner {resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as exc:
        last_error = f"forward transport error: {exc}"

    async with SessionLocal() as session, session.begin():
        row = await session.get(RelayLog, log_id)
        if row is not None:
            row.attempts += 1
            now = datetime.now(UTC)
            if forwarded_ok:
                row.status = RelayStatus.DELIVERED
                row.forwarded_at = now
                row.delivered_at = now
                row.last_error = None
            else:
                row.status = RelayStatus.FAILED
                row.forwarded_at = now
                row.last_error = last_error

    if not forwarded_ok:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, last_error or "forward failed"
        )
    # ACCEPTED-NEW: a genuine first delivery the partner accepted. The dispatcher
    # may ACK the outbox row only on this (delivered=True, duplicate=False).
    return JSONResponse(
        {
            "ic_txn_id": str(ic_txn_id),
            "status": "DELIVERED",
            "duplicate": False,
            "delivered": True,
        }
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _hash_token(cleartext: str | None) -> str | None:
    if not cleartext:
        return None
    return bcrypt.hashpw(cleartext.encode("utf-8"), bcrypt.gensalt(10)).decode("ascii")


def _verify_token(cleartext: str, token_hash: str) -> bool:
    if not cleartext:
        return False
    try:
        return bcrypt.checkpw(cleartext.encode("utf-8"), token_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _resolve_dst_token(edge_id: uuid.UUID) -> str | None:
    """Resolve the DST per-edge token cleartext for the forward.

    Production sources this from the broker's secret store (keyed by edge). The
    broker DB stores only the hash, so this is a secret-wiring seam filled at
    go-live; until then forwarding presents no token (a same-LAN deployment may
    rely on the signature alone, but the receiver requires a token, so go-live
    MUST wire this). Returns None here.
    """
    return None
