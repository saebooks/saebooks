"""Stripe webhook router — mounted at ``/webhooks/stripe``.

This is the *authoritative* location for Stripe's event delivery.
The legacy handler in ``saebooks/routers/integrations.py`` will be
removed by the Cat-C rollup; this module is its replacement and the
version that will survive.

Why not under /api/v1/?
-----------------------
Stripe webhook URLs are registered once in the Stripe Dashboard
(or via the Stripe API) and don't change. Putting them under a
versioned prefix would imply we'll break the URL on every API version
bump — which we won't. The ``/webhooks/stripe`` path is stable by
design.

Authentication
--------------
**HMAC signature only** — the Stripe-Signature header is verified
against ``STRIPE_WEBHOOK_SECRET`` before any event processing.
No Bearer token is required (and none is sent). The endpoint is
therefore excluded from ``ForwardAuthMiddleware`` via the
``/webhooks/`` prefix in ``OPEN_PATH_PREFIXES``.

Events handled
--------------
The handler here processes events that affect the *licence lifecycle*
of a SAE Books instance:

* ``checkout.session.completed`` — handled by ``saebooks/api/v1/billing.py``
  (the billing router wires the public Checkout flow). This module
  handles the complementary subscription management events:
* ``customer.subscription.updated`` — update tenant edition from
  subscription metadata; set to community if status is past_due or
  canceled.
* ``customer.subscription.deleted`` — revert tenant edition to
  community, clear ``stripe_subscription_id``.

Intentionally NOT handled here
-------------------------------
``payment_intent.succeeded`` events for *customer-owned* Stripe
accounts are out of scope — customers' own Stripe webhooks would
target their own infrastructure (or the customer_stripe OAuth flow),
not SAE Engineering's webhook endpoint. The ``payment_intent.succeeded``
handler in ``services/integrations/stripe.py`` deals with one-off
invoice payments in the *legacy monolith* data model; the rebuild API
doesn't inherit that pattern.

Idempotency
-----------
Every event carries a unique ``id``. We do NOT yet persist a
processed-event log (a future migration can add one) — for now,
idempotency relies on the upstream model state being idempotent
(setting edition = 'community' twice is safe).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services.integrations.stripe import (
    StripeError,
    StripeSignatureError,
    parse_event,
    verify_signature,
)

logger = logging.getLogger("saebooks.webhooks.stripe")

# NOTE: no prefix here — main.py mounts this router at /webhooks/stripe.
router = APIRouter(tags=["webhooks"])

_SUBSCRIPTION_ACTIVE_STATUSES = frozenset({"active", "trialing"})
_SUBSCRIPTION_LAPSED_STATUSES = frozenset({"past_due", "canceled", "unpaid", "paused"})


async def _handle_subscription_updated(event: dict[str, Any]) -> bool:
    """Process ``customer.subscription.updated``.

    Updates the tenant's edition based on the subscription's
    ``metadata.sae_edition`` and its ``status``. Lapsed statuses
    revert the tenant to community edition.

    Returns ``True`` when a tenant was found and updated.
    """
    from saebooks.models.tenant import Tenant  # noqa: PLC0415

    data = event.get("data") or {}
    subscription = data.get("object") or {}
    sub_id = subscription.get("id", "")
    sub_status = subscription.get("status", "")
    metadata = subscription.get("metadata") or {}
    edition = metadata.get("sae_edition", "")

    if not sub_id:
        logger.warning("stripe webhook: subscription.updated missing id")
        return False

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.stripe_subscription_id == sub_id)
        )
        tenant = result.scalars().first()
        if tenant is None:
            logger.info(
                "stripe webhook: subscription %s not linked to a tenant — skipping",
                sub_id,
            )
            return False

        if sub_status in _SUBSCRIPTION_LAPSED_STATUSES:
            logger.info(
                "stripe webhook: subscription %s status=%s — reverting tenant=%s to community",
                sub_id, sub_status, tenant.id,
            )
            tenant.edition = "community"
        elif sub_status in _SUBSCRIPTION_ACTIVE_STATUSES and edition in (
            "business", "pro", "enterprise"
        ):
            logger.info(
                "stripe webhook: subscription %s — setting tenant=%s edition=%s",
                sub_id, tenant.id, edition,
            )
            tenant.edition = edition

        await session.commit()
    return True


async def _handle_subscription_deleted(event: dict[str, Any]) -> bool:
    """Process ``customer.subscription.deleted``.

    Reverts tenant to community edition and clears
    ``stripe_subscription_id``.
    """
    from saebooks.models.tenant import Tenant  # noqa: PLC0415

    data = event.get("data") or {}
    subscription = data.get("object") or {}
    sub_id = subscription.get("id", "")

    if not sub_id:
        logger.warning("stripe webhook: subscription.deleted missing id")
        return False

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.stripe_subscription_id == sub_id)
        )
        tenant = result.scalars().first()
        if tenant is None:
            logger.info(
                "stripe webhook: subscription %s not linked to a tenant — skipping",
                sub_id,
            )
            return False

        logger.info(
            "stripe webhook: subscription %s deleted — reverting tenant=%s to community",
            sub_id, tenant.id,
        )
        tenant.edition = "community"
        tenant.stripe_subscription_id = None
        await session.commit()
    return True


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Receive and process a Stripe event.

    Always returns 2xx on valid signatures — including for events we
    choose to ignore — so Stripe does not retry. Non-2xx is reserved
    for signature failures and configuration errors that warrant retry.

    Returns:
        200 ``{"received": true, "handled": <bool>}``
        400 on signature failure or malformed payload.
        503 when STRIPE_WEBHOOK_SECRET is not configured.
    """
    if not settings.stripe_webhook_secret:
        return JSONResponse(
            {"error": "Stripe webhook not configured"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not sig_header:
        return JSONResponse(
            {"error": "Missing Stripe-Signature header"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        verify_signature(raw, sig_header, settings.stripe_webhook_secret)
    except StripeSignatureError as exc:
        logger.warning("stripe webhook: signature verification failed: %s", exc)
        return JSONResponse(
            {"error": "Signature verification failed"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        event = parse_event(raw)
    except StripeError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    etype = event.get("type", "")
    logger.info(
        "stripe webhook: received event id=%s type=%s",
        event.get("id"),
        etype,
    )

    handled: bool = False

    if etype == "customer.subscription.updated":
        handled = await _handle_subscription_updated(event)
    elif etype == "customer.subscription.deleted":
        handled = await _handle_subscription_deleted(event)
    else:
        logger.debug("stripe webhook: unhandled event type %s — ack'd", etype)

    return JSONResponse({"received": True, "handled": handled})


__all__ = ["router"]
