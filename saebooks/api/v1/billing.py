"""``/api/v1/billing`` — PUBLIC SHIM (commercial control plane stubbed).

Stripe Checkout / Customer-Portal / subscription-webhook are the commercial
billing control plane and are NOT shipped in the open engine. The router is
preserved (the API assembler mounts ``billing.router`` at ``/billing``) so the
endpoints exist and answer honestly with 501 instead of a bare 404. A
self-hoster manages editions via ``SAEBOOKS_EDITION`` (see the licence resolver
shim), not via Stripe.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/billing", tags=["billing"])

_NOT_AVAILABLE = (
    "Billing is a commercial feature and is not available in the open engine. "
    "Manage your edition via SAEBOOKS_EDITION on a self-hosted install."
)


@router.post("/checkout-session", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def checkout_session() -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_AVAILABLE)


@router.post("/portal-session", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def portal_session() -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_AVAILABLE)


@router.post("/webhook", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def stripe_webhook() -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _NOT_AVAILABLE)


__all__ = ["router"]
