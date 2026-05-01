"""Stripe billing endpoints — public Checkout + webhook.

Two endpoints:

* ``POST /billing/checkout-session`` — auth + email-verified.
  Body: ``{edition: 'business'|'pro'}``. Response: ``{checkout_url}``.
  The web frontend redirects the browser to that URL.

* ``POST /billing/webhook`` — public, signature-verified.
  Handles three Stripe events:

  - ``checkout.session.completed`` → set the buyer's tenant edition.
    If the buyer has a verified user account → attach to their
    existing tenant. Otherwise → mint a fresh tenant + owner user
    (unverified password, magic-link sent).
  - ``customer.subscription.updated`` → update edition based on
    metadata + status (past_due/canceled → community).
  - ``customer.subscription.deleted`` → revert edition to community.

Tenant resolution for subscription events: by ``stripe_subscription_id``
column. We persist that on ``checkout.session.completed`` so the later
events can find the tenant without going via email (which the user
might change).

Why not the existing stripe.py?
-------------------------------
``services/integrations/stripe.py`` handles ``payment_intent.succeeded``
for one-off invoice payments — different event family, different
business logic. We share the signature-verification helper from that
module.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from saebooks.api.v1.auth import (
    require_bearer,
    require_email_verified,
    resolve_tenant_id,
)
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.auth_tokens import (
    expiry_for,
    generate_token,
    hash_token,
)
from saebooks.services.integrations.stripe import (
    StripeError,
    StripeNotConfiguredError,
    StripeSignatureError,
    parse_event,
    verify_signature,
)
from saebooks.services.integrations.stripe_billing import (
    EDITIONS,
    StripeBillingError,
    create_checkout_session,
    create_portal_session,
)
from saebooks.services.mailer import send_email

logger = logging.getLogger("saebooks.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# POST /billing/checkout-session
# ---------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    edition: Literal["business", "pro"]


class CheckoutResponse(BaseModel):
    checkout_url: str


@router.post(
    "/checkout-session",
    response_model=CheckoutResponse,
    dependencies=[Depends(require_bearer), Depends(require_email_verified)],
)
async def checkout_session(
    body: CheckoutRequest,
    request: Request,
) -> CheckoutResponse:
    """Mint a Stripe Checkout Session URL for the authenticated user."""
    user = getattr(request.state, "user", None)
    if user is None or not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authenticated user has no email on file",
        )
    try:
        result = await create_checkout_session(body.edition, user.email)
    except StripeBillingError as exc:
        logger.error("checkout_session: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured — contact support.",
        ) from exc
    return CheckoutResponse(checkout_url=result["checkout_url"])


# ---------------------------------------------------------------------------
# POST /billing/portal-session
# ---------------------------------------------------------------------------


class PortalResponse(BaseModel):
    portal_url: str


@router.post(
    "/portal-session",
    response_model=PortalResponse,
    dependencies=[Depends(require_bearer), Depends(require_email_verified)],
)
async def portal_session(request: Request) -> PortalResponse:
    """Mint a Stripe Customer Portal session for the authenticated tenant."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    async with AsyncSessionLocal() as session:
        tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found for this account.",
        )
    try:
        result = await create_portal_session(tenant.stripe_customer_id)
    except StripeBillingError as exc:
        logger.error("portal_session: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing portal is not available — contact support.",
        ) from exc
    return PortalResponse(portal_url=result["portal_url"])


# ---------------------------------------------------------------------------
# POST /billing/webhook
# ---------------------------------------------------------------------------


def _edition_from_event(event: dict[str, Any]) -> str | None:
    """Pull the ``sae_edition`` metadata from the event object,
    defensively walking the structure Stripe sends.

    For checkout.session.completed: object is the session,
    metadata is on the session AND mirrored onto subscription_data
    when we created the session.

    For subscription events: object is the subscription, metadata is
    set there directly (we set it via ``subscription_data.metadata``
    when creating the checkout session).
    """
    obj = (event.get("data") or {}).get("object") or {}
    md = obj.get("metadata") or {}
    edition = md.get("sae_edition")
    if edition in ("business", "pro", "enterprise", "community"):
        return edition
    return None


async def _find_user_by_email(session: Any, email: str) -> User | None:
    if not email:
        return None
    result = await session.execute(
        select(User).where(User.email.ilike(email))
    )
    return result.scalars().first()


async def _find_tenant_by_subscription(session: Any, subscription_id: str) -> Tenant | None:
    if not subscription_id:
        return None
    result = await session.execute(
        select(Tenant).where(Tenant.stripe_subscription_id == subscription_id)
    )
    return result.scalars().first()


async def _handle_checkout_completed(event: dict[str, Any]) -> None:
    """Wire up a successful checkout.

    Two paths:

    * Buyer already has a verified user account → set their tenant's
      edition + Stripe IDs. Don't mint a new tenant (would orphan
      their books).
    * Buyer is new → mint tenant + owner user (no password, marked
      verified-via-Stripe), email a magic-link "set your password".
    """
    obj = (event.get("data") or {}).get("object") or {}
    customer_email = obj.get("customer_email") or obj.get("customer_details", {}).get("email") or ""
    customer_email = customer_email.strip()
    edition = _edition_from_event(event) or "business"
    customer_id = obj.get("customer") or ""
    subscription_id = obj.get("subscription") or ""

    if not customer_email or not subscription_id:
        logger.warning(
            "billing/webhook checkout.completed: missing customer_email/subscription (id=%s)",
            obj.get("id"),
        )
        return

    async with AsyncSessionLocal() as session:
        user = await _find_user_by_email(session, customer_email)

        if user is not None and user.email_verified_at is not None:
            # Attach to existing tenant.
            tenant = await session.get(Tenant, user.tenant_id)
            if tenant is None:
                logger.error(
                    "billing/webhook: user %s has missing tenant %s",
                    user.id,
                    user.tenant_id,
                )
                return
            tenant.edition = edition
            tenant.stripe_customer_id = customer_id or tenant.stripe_customer_id
            tenant.stripe_subscription_id = subscription_id
            await session.commit()
            logger.info(
                "billing/webhook: attached subscription %s (edition=%s) to existing tenant %s",
                subscription_id,
                edition,
                tenant.id,
            )
            return

        # Mint new tenant + owner user.
        tenant_name = customer_email.split("@", 1)[0]
        tenant_slug = f"{tenant_name}-{uuid.uuid4().hex[:6]}"
        tenant = Tenant(
            id=uuid.uuid4(),
            name=tenant_name,
            slug=tenant_slug,
            edition=edition,
            stripe_customer_id=customer_id or None,
            stripe_subscription_id=subscription_id,
        )
        session.add(tenant)
        await session.flush()

        username = customer_email.lower()
        existing_username = await session.execute(
            select(User).where(User.username == username)
        )
        if existing_username.scalars().first() is not None:
            username = f"{username}-{uuid.uuid4().hex[:4]}"

        raw_token = generate_token()
        new_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            username=username,
            email=customer_email,
            display_name=tenant_name,
            role="owner",
            password_hash=None,
            email_verified_at=datetime.now(UTC),
            magic_link_token_hash=hash_token(raw_token),
            magic_link_expires_at=expiry_for("magic"),
            password_version=0,
            version=1,
        )
        session.add(new_user)
        await session.commit()

    try:
        await send_email(
            customer_email,
            "Your SAE Books account is ready",
            f"""<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;color:#1f2937;">
<h2 style="color:#194291;">Welcome to SAE Books</h2>
<p>Your <strong>{edition.title()}</strong> subscription is active. Click below to sign in and set your password.</p>
<p><a href="https://app.saebooks.com.au/magic-link?token={raw_token}" style="display:inline-block;background:#194291;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">Sign in</a></p>
<p style="font-size:12px;color:#6b7280;">This link expires in 15 minutes. Request a new one at app.saebooks.com.au/forgot-password if you miss the window.</p>
</body></html>""",
        )
    except Exception as exc:
        logger.error("billing/webhook: post-checkout email send failed for %s: %s", customer_email, exc)


async def _handle_subscription_updated(event: dict[str, Any]) -> None:
    obj = (event.get("data") or {}).get("object") or {}
    sub_id = obj.get("id") or ""
    status_str = (obj.get("status") or "").lower()
    edition = _edition_from_event(event) or "business"

    async with AsyncSessionLocal() as session:
        tenant = await _find_tenant_by_subscription(session, sub_id)
        if tenant is None:
            logger.warning(
                "billing/webhook subscription.updated: no tenant for %s", sub_id
            )
            return
        if status_str in {"active", "trialing"}:
            tenant.edition = edition
        elif status_str in {"past_due", "canceled", "unpaid", "incomplete_expired"}:
            tenant.edition = "community"
        # leave other states (incomplete, paused) alone — they're transient
        await session.commit()
    logger.info(
        "billing/webhook subscription.updated: tenant=%s edition=%s status=%s",
        tenant.id,
        tenant.edition,
        status_str,
    )


async def _handle_subscription_deleted(event: dict[str, Any]) -> None:
    obj = (event.get("data") or {}).get("object") or {}
    sub_id = obj.get("id") or ""

    async with AsyncSessionLocal() as session:
        tenant = await _find_tenant_by_subscription(session, sub_id)
        if tenant is None:
            logger.warning(
                "billing/webhook subscription.deleted: no tenant for %s", sub_id
            )
            return
        tenant.edition = "community"
        tenant.stripe_subscription_id = None
        await session.commit()
    logger.info(
        "billing/webhook subscription.deleted: reverted tenant %s to community",
        tenant.id,
    )


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    """Stripe-signed webhook receiver.

    Returns 200 on every well-handled event so Stripe doesn't retry.
    Non-2xx for signature failures, 503 if not configured. Unknown
    event types ack with ``{ignored: true}``.
    """
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header",
        )
    secret = settings.stripe_webhook_secret.strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhook secret is not configured",
        )

    try:
        verify_signature(payload, stripe_signature, secret)
    except (StripeSignatureError, StripeNotConfiguredError) as exc:
        logger.warning("billing/webhook: signature verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature",
        ) from exc

    try:
        event = parse_event(payload)
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    etype = event.get("type") or ""
    try:
        if etype == "checkout.session.completed":
            await _handle_checkout_completed(event)
        elif etype == "customer.subscription.updated":
            await _handle_subscription_updated(event)
        elif etype == "customer.subscription.deleted":
            await _handle_subscription_deleted(event)
        else:
            logger.info("billing/webhook: ignoring event type %s", etype)
            return {"ignored": True, "type": etype}
    except Exception:
        # Re-raise so Stripe retries — but log first.
        logger.exception("billing/webhook: handler for %s raised", etype)
        raise

    return {"received": True, "type": etype}


# Used by /billing/checkout-success on the web side.
__all__ = ["EDITIONS", "router", "resolve_tenant_id"]
