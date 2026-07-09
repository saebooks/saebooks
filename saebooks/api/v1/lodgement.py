"""``/api/v1/lodgement`` — admin-only smoke endpoint for the relay.

The full STP/BAS routes (which actually build envelopes and call
``LodgementService.lodge_stp`` / ``lodge_bas``) come with the STP UI
build. For now we expose a single probe so Richard can sanity-check
the wire saebooks-api → lodge.saebooks.com.au end-to-end without
needing the rest of the UI.

The probe calls ``my_audit_log(limit=1)``. On a Pro+ install with a
valid licence token this proves:

1. Factory dispatched to ``RemoteLodgementService``.
2. ``LicenseService.current_token()`` returned a usable token.
3. Lodge-server accepted the token (no 401/403).
4. The audit endpoint returned JSON.

On community/offline it returns 402 (Payment Required) — the
``NullLodgementService`` raises ``LodgementUnsupportedEdition`` and
we map that to a 402 with a human message naming the upgrade path.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_lodgement
from saebooks.services.lodgement import (
    LodgementAuthError,
    LodgementEditionError,
    LodgementService,
    LodgementUnsupportedEdition,
    LodgementUpstreamUnavailable,
)
from saebooks.services.lodgement.remote import RemoteLodgementService

log = logging.getLogger(__name__)


router = APIRouter(
    prefix="/lodgement",
    tags=["lodgement"],
    dependencies=[Depends(require_bearer)],
)


async def _require_admin_inline(request: Request) -> None:
    """Admin gate matching the pattern in users.py.

    Inline import to avoid a circular: users.py imports auth, deps,
    schemas, and is the canonical home of the gate. We mirror its
    rule (JWT user role >= ADMIN, or static-token + ``X-Admin: true``)
    rather than importing because the probe is so small that pulling
    in the whole users module is the wrong shape.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        # JWT bearer with a resolved user — enforce role >= ADMIN.
        role = getattr(request.state, "role", None)
        # ``role`` is a string-valued enum in the live system. Any
        # value containing 'admin' (admin / superadmin / owner) clears.
        if role is None or "admin" not in str(role).lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required",
            )
        return

    # Static-token path — fall back to the X-Admin header convention.
    if request.headers.get("X-Admin", "").lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required (X-Admin: true)",
        )


@router.get("/probe", dependencies=[Depends(_require_admin_inline)])
async def probe(
    svc: LodgementService = Depends(get_lodgement),
) -> dict[str, Any]:
    """Smoke test: ``my_audit_log(limit=1)`` against lodge-server.

    Shape:

    * ``ok: true``, ``impl: "remote"``, ``recent: [...]`` on success.
    * 402 with detail string when the active licence has no
      ``ato_sbr`` flag (NullLodgementService refused).
    * 401 / 403 / 502 propagated from the relay errors.
    """
    impl_name = type(svc).__name__
    is_remote = isinstance(svc, RemoteLodgementService)
    try:
        recent = await svc.my_audit_log(limit=1)
    except LodgementUnsupportedEdition as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc
    except LodgementAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
        ) from exc
    except LodgementEditionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=exc.detail,
        ) from exc
    except LodgementUpstreamUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.detail,
        ) from exc
    return {
        "ok": True,
        "impl": "remote" if is_remote else impl_name,
        "recent": recent,
    }
