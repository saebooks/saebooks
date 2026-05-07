"""Customer-facing Stripe Connect integration.

This module is for *customer-owned* Stripe accounts that a SAE Books
tenant connects to their books — i.e. the customer's Stripe account,
not SAE Engineering's Stripe account used for licence billing.

Contrast with ``stripe_billing.py`` which handles SAE Engineering
billing customers for SAE Books licences (product/price bootstrap,
Checkout Session creation for licence purchase, Billing Portal).

Customer Stripe Connect flow
----------------------------
1. Customer clicks "Connect Stripe" in Settings → Integrations.
2. ``initiate_connect_oauth`` returns an OAuth ``authorize_url`` plus
   an opaque ``state`` token (stored in the tenant's settings row).
3. Customer authorises on Stripe, which redirects to the SAE Books
   OAuth callback URL with ``code`` + ``state``.
4. Callback handler (future build) calls ``exchange_oauth_code`` to
   swap the code for an access token and persists the ``stripe_user_id``
   (the connected account ID, ``acct_...``).
5. Subsequent API calls to the customer's Stripe data use the
   ``Stripe-Account: acct_...`` header.

State storage
-------------
The ``state`` value is a random hex token written to the
``stripe_connect_state`` column on the tenant row (added by a future
migration when the OAuth callback is implemented). It is validated on
callback to prevent CSRF. The state is cleared on success or expiry.

Configuration
-------------
``STRIPE_CLIENT_ID`` — the Stripe platform application's client ID
  (``ca_...``). Distinct from ``STRIPE_SECRET_KEY`` which is SAE
  Engineering's account key.  When unset, ``initiate_connect_oauth``
  raises ``CustomerStripeNotConfiguredError``.
``STRIPE_CONNECT_REDIRECT_URI`` — the absolute URL Stripe sends the
  customer back to after authorisation (must be registered in the
  Stripe Dashboard → Platform settings → Redirect URIs).

Gate
----
``FLAG_STRIPE_INTEGRATION`` (Business+) in
``saebooks.services.features``. The router applies
``Depends(require_feature(FLAG_STRIPE_INTEGRATION))``; this service
module does not enforce the flag directly.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("saebooks.customer_stripe")

_STRIPE_OAUTH_BASE = "https://connect.stripe.com"
_STRIPE_API_BASE = "https://api.stripe.com/v1"

# Default scopes requested during Connect OAuth. ``read_write`` lets
# us create payment links / invoices on behalf of the customer;
# ``read_only`` would suffice for display-only enrichment. We use
# ``read_write`` so future phases can push invoices to the customer's
# Stripe without re-connecting.
_CONNECT_SCOPE = "read_write"


class CustomerStripeError(RuntimeError):
    """Base class for customer Stripe errors."""


class CustomerStripeNotConfiguredError(CustomerStripeError):
    """Raised when STRIPE_CLIENT_ID is not set."""


class CustomerStripeOAuthError(CustomerStripeError):
    """Raised when the Stripe OAuth token exchange fails."""


def _client_id() -> str:
    """Resolve ``STRIPE_CLIENT_ID`` from environment.

    Raises ``CustomerStripeNotConfiguredError`` when unset so callers
    get a clean error rather than a cryptic redirect to Stripe with an
    empty client_id parameter.
    """
    cid = os.environ.get("STRIPE_CLIENT_ID", "").strip()
    if not cid:
        raise CustomerStripeNotConfiguredError(
            "STRIPE_CLIENT_ID is not set — cannot initiate Stripe Connect."
        )
    return cid


def _secret_key() -> str:
    """Resolve SAE Engineering's Stripe secret key for token exchange."""
    from saebooks.config import settings  # noqa: PLC0415 — late to avoid circular

    key = (settings.stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY", "")).strip()
    if not key:
        raise CustomerStripeNotConfiguredError(
            "STRIPE_SECRET_KEY is not set — cannot exchange OAuth code."
        )
    return key


def initiate_connect_oauth(
    tenant_id: uuid.UUID,
    *,
    redirect_uri: str = "",
    scope: str = _CONNECT_SCOPE,
) -> dict[str, str]:
    """Build the Stripe Connect OAuth authorisation URL.

    Returns ``{authorize_url, state}``. The caller must persist
    ``state`` on the tenant row so the callback can validate it.

    Args:
        tenant_id:    The tenant initiating the connection (used as a
                      ``user_id`` hint for Stripe's account picker —
                      not persisted at Stripe, just a UX hint).
        redirect_uri: The absolute callback URL. Falls back to
                      ``STRIPE_CONNECT_REDIRECT_URI`` env var.  Stripe
                      requires the value to exactly match a registered
                      URI in the platform settings.
        scope:        OAuth scope — ``'read_write'`` (default) or
                      ``'read_only'``.

    Returns:
        ``{"authorize_url": "<stripe oauth url>", "state": "<random hex>"}``
    """
    client_id = _client_id()
    state = secrets.token_hex(24)

    effective_redirect = redirect_uri or os.environ.get(
        "STRIPE_CONNECT_REDIRECT_URI", ""
    )
    if not effective_redirect:
        raise CustomerStripeNotConfiguredError(
            "No redirect_uri provided and STRIPE_CONNECT_REDIRECT_URI is not set."
        )

    params: dict[str, str] = {
        "client_id": client_id,
        "scope": scope,
        "response_type": "code",
        "state": state,
        "redirect_uri": effective_redirect,
        "stripe_user[business_type]": "company",
        "suggested_capabilities[]": "card_payments",
    }
    # ``user_id`` metadata — Stripe shows this in the platform
    # dashboard to identify which of our tenants connected.
    params["stripe_landing"] = "login"

    authorize_url = f"{_STRIPE_OAUTH_BASE}/oauth/authorize?{urlencode(params)}"
    logger.info(
        "customer_stripe: initiated OAuth for tenant=%s state=%s",
        tenant_id,
        state[:8] + "...",
    )
    return {"authorize_url": authorize_url, "state": state}


async def exchange_oauth_code(
    code: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange an OAuth ``code`` for a Stripe access token.

    Called from the OAuth callback handler after the ``state`` has
    been validated.  Returns the full Stripe token response dict,
    which includes ``stripe_user_id`` (the connected account ID,
    ``acct_...``), ``access_token``, and ``scope``.

    Raises ``CustomerStripeOAuthError`` on upstream failure.
    """
    secret_key = _secret_key()
    owned = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http.post(
            f"{_STRIPE_API_BASE}/oauth/token",
            content=urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(secret_key, ""),
        )
        if resp.status_code >= 400:
            raise CustomerStripeOAuthError(
                f"Stripe OAuth token exchange failed: {resp.status_code} {resp.text}"
            )
        data = resp.json()
        logger.info(
            "customer_stripe: token exchange succeeded, stripe_user_id=%s",
            data.get("stripe_user_id"),
        )
        return data
    finally:
        if owned:
            await http.aclose()


async def get_account_status(
    stripe_account_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the connected account object from Stripe.

    Returns a subset: ``{id, business_profile, charges_enabled,
    payouts_enabled, details_submitted}``.  Returns ``{}`` when
    ``stripe_account_id`` is empty (not yet connected).
    """
    if not stripe_account_id:
        return {}

    secret_key = _secret_key()
    owned = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http.get(
            f"{_STRIPE_API_BASE}/accounts/{stripe_account_id}",
            auth=(secret_key, ""),
            headers={"Stripe-Account": stripe_account_id},
        )
        if resp.status_code == 404:
            logger.warning(
                "customer_stripe: account %s not found at Stripe",
                stripe_account_id,
            )
            return {}
        if resp.status_code >= 400:
            raise CustomerStripeError(
                f"Stripe account fetch failed: {resp.status_code} {resp.text}"
            )
        raw = resp.json()
        return {
            "id": raw.get("id"),
            "business_profile": raw.get("business_profile") or {},
            "charges_enabled": raw.get("charges_enabled", False),
            "payouts_enabled": raw.get("payouts_enabled", False),
            "details_submitted": raw.get("details_submitted", False),
        }
    finally:
        if owned:
            await http.aclose()


__all__ = [
    "CustomerStripeError",
    "CustomerStripeNotConfiguredError",
    "CustomerStripeOAuthError",
    "exchange_oauth_code",
    "get_account_status",
    "initiate_connect_oauth",
]
