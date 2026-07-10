"""``/api/v1/license`` — PUBLIC SHIM (portal operations stubbed).

The read-only snapshot stays available (it reports the community/self-selected
edition via the public ``LicenseService``). The portal-bound operations — upload
a portal JWT, refresh against the license-server, and the launch-promo counter —
are commercial control-plane calls and answer 501 / disabled in the open engine.

Both router attributes the API assembler mounts (``router`` and
``_promo_router``, each at ``/license``) are preserved.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from saebooks.api.v1.auth import require_bearer
from saebooks.services.licence import LicenseService

router = APIRouter(
    prefix="/license",
    tags=["license"],
    dependencies=[Depends(require_bearer)],
)

_NOT_AVAILABLE = (
    "Portal licence operations are a commercial feature and are not available "
    "in the open engine. Set SAEBOOKS_EDITION to select an edition."
)


@router.get("/snapshot")
async def get_snapshot() -> dict[str, Any]:
    """Return the current resolved licence snapshot (community/self-selected)."""
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


@router.post("/upload", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def upload_token() -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_AVAILABLE)


@router.post("/refresh", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def refresh_token() -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_AVAILABLE)


_promo_router = APIRouter(prefix="/license", tags=["license"])


@_promo_router.get("/promo-stats")
async def promo_stats() -> dict:
    """Launch promo is a commercial control-plane feature — disabled here."""
    return {"enabled": False, "issued": 0, "limit": 0, "remaining": 0}


__all__ = ["_promo_router", "router"]
