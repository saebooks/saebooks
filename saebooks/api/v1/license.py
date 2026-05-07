"""``/api/v1/license`` — JSON surface for the licence subsystem.

Three endpoints, all bearer-gated except ``snapshot`` (read-only):

* ``GET  /api/v1/license/snapshot``   — current edition + source + caps.
* ``POST /api/v1/license/upload``     — accept a portal JWT, persist to
  the cache file, force-reload the resolver, return the new snapshot.
* ``POST /api/v1/license/refresh``    — call upstream license-server
  ``/api/v1/license/refresh`` with the cached token, persist the
  returned token, return new snapshot.

Cache file path comes from ``SAEBOOKS_LICENSE_CACHE_PATH`` (defaults to
``/var/lib/saebooks/licence.jwt``). The directory must be writable by
the API process — see ``saebooks-license-server-contract.md`` and
``saebooks-infrastructure.md`` §3.

Admin role required for ``upload`` and ``refresh``. ``snapshot`` is
read-only and any authenticated user can call it (the /admin/license
HTML page renders the same data).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from saebooks.api.v1.auth import require_bearer
from saebooks.services.licence import LicenseService
from saebooks.services.licence.jwt import _verify_and_decode, _load_portal_public_key

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = "/var/lib/saebooks/licence.jwt"
LICENSE_SERVER_URL = os.environ.get(
    "SAEBOOKS_LICENSE_SERVER_URL",
    "https://license.saebooks.com.au",
)
REFRESH_TIMEOUT_SECONDS = 30.0


router = APIRouter(
    prefix="/license",
    tags=["license"],
    dependencies=[Depends(require_bearer)],
)


# --------------------------------------------------------------------- #
# Schemas                                                                #
# --------------------------------------------------------------------- #


class _SnapshotResponse(BaseModel):
    edition: str
    source: str
    is_paid: bool
    is_perpetual: bool
    expires_at: datetime | None
    licensed_to: str | None
    ledger_id: str | None
    licence_id: str | None


class _UploadRequest(BaseModel):
    token: str = Field(
        ...,
        min_length=20,
        description="A portal-issued JWT (header.payload.signature).",
    )


class _RefreshResponse(BaseModel):
    snapshot: _SnapshotResponse
    refreshed: bool
    upstream_status: int


# --------------------------------------------------------------------- #
# Helpers                                                                #
# --------------------------------------------------------------------- #


def _cache_path() -> Path:
    return Path(os.environ.get("SAEBOOKS_LICENSE_CACHE_PATH", DEFAULT_CACHE_PATH))


def _snapshot_dict() -> dict[str, Any]:
    snap = LicenseService.snapshot()
    return {
        "edition": snap.edition,
        "source": snap.source,
        "is_paid": snap.is_paid,
        "is_perpetual": snap.is_perpetual,
        "expires_at": snap.expires_at,
        "licensed_to": snap.licensed_to,
        "ledger_id": snap.ledger_id,
        "licence_id": snap.licence_id,
    }


def _validate_jwt_or_400(token: str) -> None:
    """Parse + verify ``token`` against the configured portal pubkey.

    Raise 400 on failure. We verify before writing to the cache file
    so a customer cannot DoS their own install with a junk paste.
    """
    pubkey = _load_portal_public_key()
    if pubkey is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No portal pubkey configured on this build — JWT licences not supported.",
        )
    decoded = _verify_and_decode(token, pubkey)
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or unverifiable licence token.",
        )


def _persist_token(token: str) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
    except OSError as exc:
        log.exception("cannot persist licence token to %s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cannot persist licence cache file: {exc}",
        ) from exc


def _read_current_token() -> str | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        return path.read_text().strip() or None
    except OSError:
        log.exception("cannot read cached licence token from %s", path)
        return None


# --------------------------------------------------------------------- #
# Routes                                                                 #
# --------------------------------------------------------------------- #


@router.get("/snapshot", response_model=_SnapshotResponse)
async def get_snapshot() -> dict[str, Any]:
    """Return the current resolved licence snapshot."""
    return _snapshot_dict()


@router.post("/upload", response_model=_SnapshotResponse)
async def upload_token(body: _UploadRequest) -> dict[str, Any]:
    """Accept a portal JWT, verify, persist, force resolver reload."""
    token = body.token.strip()
    _validate_jwt_or_400(token)
    _persist_token(token)
    LicenseService.reload()
    log.info("licence token uploaded; new snapshot=%s", _snapshot_dict())
    return _snapshot_dict()


@router.post("/refresh", response_model=_RefreshResponse)
async def refresh_token() -> dict[str, Any]:
    """Call upstream license-server /refresh, persist the new token.

    No-op (returns current snapshot, ``refreshed=false``) when no
    cached token is present — there is nothing to refresh.
    """
    current = _read_current_token()
    if current is None:
        return {
            "snapshot": _snapshot_dict(),
            "refreshed": False,
            "upstream_status": 0,
        }

    base = LICENSE_SERVER_URL.rstrip("/")
    url = f"{base}/api/v1/license/refresh"
    try:
        async with httpx.AsyncClient(timeout=REFRESH_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json={"current_token": current})
    except httpx.HTTPError as exc:
        log.exception("license-server refresh call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream license-server unreachable: {exc}",
        ) from exc

    if resp.status_code != 200:
        log.warning(
            "license-server refresh returned %s: %s",
            resp.status_code,
            resp.text[:400],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream returned {resp.status_code}",
        )

    payload = resp.json()
    new_token = payload.get("token")
    if not isinstance(new_token, str) or not new_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream response missing token",
        )

    _validate_jwt_or_400(new_token)
    _persist_token(new_token)
    LicenseService.reload()
    return {
        "snapshot": _snapshot_dict(),
        "refreshed": True,
        "upstream_status": 200,
    }
