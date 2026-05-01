"""Stripe outbound integration for the public-tier checkout flow.

Implemented against Stripe's REST API directly via ``httpx`` — we
avoid the Stripe Python SDK because it's heavy (~MB), introduces a
new global state pattern, and we only need three endpoints (products,
prices, checkout sessions). The existing webhook signature
verification in ``services/integrations/stripe.py`` is also stdlib-only.

Two responsibilities:

1. ``ensure_products()`` — idempotently creates the Business and Pro
   recurring prices in Stripe. Searches by metadata sentinel
   ``sae_edition`` so re-running the script doesn't double-up.
2. ``create_checkout_session(edition, customer_email)`` — builds a
   Stripe Checkout Session and returns the hosted-checkout URL.

Live/test guard
---------------
``STRIPE_LIVE != "1"`` requires the secret key to start with ``sk_test_``,
otherwise we abort. Protects an instance that accidentally booted with
a live key.

Pricing
-------
AUD; cents in ``EDITIONS``. Display strings match the marketing site.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx

from saebooks.config import settings

logger = logging.getLogger("saebooks.stripe_billing")

Edition = Literal["business", "pro"]

EDITIONS: dict[str, dict[str, Any]] = {
    "business": {
        "name": "SAE Books Business",
        "amount_cents": 4900,
        "interval": "month",
        "currency": "aud",
        "description": "Business edition (single company, up to 3 users).",
    },
    "pro": {
        "name": "SAE Books Pro",
        "amount_cents": 9900,
        "interval": "month",
        "currency": "aud",
        "description": "Pro edition (unlimited users, multi-company, STP, FX).",
    },
}

_API_BASE = "https://api.stripe.com/v1"


class StripeBillingError(RuntimeError):
    pass


class StripeBillingNotConfigured(StripeBillingError):
    pass


def _resolve_secret_key() -> str:
    key = (settings.stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY", "")).strip()
    if not key:
        raise StripeBillingNotConfigured("STRIPE_SECRET_KEY is unset")
    is_live = os.environ.get("STRIPE_LIVE", "").strip() == "1"
    if not is_live and not key.startswith("sk_test_"):
        raise StripeBillingError(
            "Refusing to use a non-test Stripe key when STRIPE_LIVE != 1 — "
            "set STRIPE_LIVE=1 explicitly to enable live mode."
        )
    if is_live and key.startswith("sk_test_"):
        logger.warning(
            "STRIPE_LIVE=1 set but secret key looks like a test key — "
            "Stripe will treat this as test mode regardless."
        )
    return key


def _client() -> httpx.AsyncClient:
    """Build an authenticated httpx client. Caller closes it."""
    return httpx.AsyncClient(
        base_url=_API_BASE,
        auth=(_resolve_secret_key(), ""),
        timeout=15.0,
    )


def _form(prefix: str, value: Any) -> list[tuple[str, str]]:
    """Encode a nested value into Stripe's bracketed form-encoded
    convention. ``prefix="metadata", value={"sae_edition":"x"}`` →
    ``[("metadata[sae_edition]", "x")]``.
    """
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_form(f"{prefix}[{k}]", v))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_form(f"{prefix}[{i}]", v))
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif value is None:
        pass
    else:
        out.append((prefix, str(value)))
    return out


def _encode_form(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in payload.items():
        out.extend(_form(k, v))
    return out


# ---------------------------------------------------------------------------
# Product / price bootstrap
# ---------------------------------------------------------------------------


async def _search(client: httpx.AsyncClient, resource: str, query: str) -> list[dict[str, Any]]:
    """Stripe ``/v1/<resource>/search`` — scoped queries by metadata."""
    resp = await client.get(f"/{resource}/search", params={"query": query})
    if resp.status_code >= 400:
        logger.error("stripe_billing: search %s failed: %s", resource, resp.text)
        resp.raise_for_status()
    return resp.json().get("data", [])


async def _list(client: httpx.AsyncClient, resource: str, **params: Any) -> list[dict[str, Any]]:
    resp = await client.get(f"/{resource}", params=params)
    if resp.status_code >= 400:
        logger.error("stripe_billing: list %s failed: %s", resource, resp.text)
        resp.raise_for_status()
    return resp.json().get("data", [])


async def _post(client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(path, data=_encode_form(payload))
    if resp.status_code >= 400:
        logger.error("stripe_billing: POST %s failed: %s", path, resp.text)
        resp.raise_for_status()
    return resp.json()


async def ensure_products() -> dict[str, str]:
    """Idempotently create products + recurring prices for each
    edition. Returns ``{edition: price_id}``.

    Search uses metadata field ``sae_edition`` which we set on every
    create — re-runs read them back instead of creating duplicates.
    """
    out: dict[str, str] = {}
    async with _client() as client:
        for edition, spec in EDITIONS.items():
            existing = await _search(
                client,
                "products",
                f"metadata['sae_edition']:'{edition}'",
            )
            if existing:
                product = existing[0]
                logger.info(
                    "stripe_billing: product %s already exists (id=%s)",
                    edition,
                    product["id"],
                )
            else:
                product = await _post(
                    client,
                    "/products",
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                        "metadata": {"sae_edition": edition},
                    },
                )
                logger.info(
                    "stripe_billing: created product %s (id=%s)",
                    edition,
                    product["id"],
                )

            prices = await _list(
                client, "prices", product=product["id"], active="true", limit=100
            )
            match: dict[str, Any] | None = None
            for p in prices:
                recurring = p.get("recurring") or {}
                if (
                    p.get("unit_amount") == spec["amount_cents"]
                    and (p.get("currency") or "").lower() == spec["currency"]
                    and recurring.get("interval") == spec["interval"]
                ):
                    match = p
                    break
            if match is None:
                match = await _post(
                    client,
                    "/prices",
                    {
                        "product": product["id"],
                        "currency": spec["currency"],
                        "unit_amount": spec["amount_cents"],
                        "recurring": {"interval": spec["interval"]},
                        "metadata": {"sae_edition": edition},
                    },
                )
                logger.info(
                    "stripe_billing: created price %s (id=%s)",
                    edition,
                    match["id"],
                )
            out[edition] = match["id"]
    return out


# ---------------------------------------------------------------------------
# Checkout session creation
# ---------------------------------------------------------------------------


async def create_checkout_session(
    edition: Edition,
    customer_email: str,
    *,
    success_url: str = "https://app.saebooks.com.au/billing/checkout-success?session_id={CHECKOUT_SESSION_ID}",
    cancel_url: str = "https://saebooks.com.au/#editions",
) -> dict[str, str]:
    """Create a Stripe Checkout Session, return ``{checkout_url, session_id}``."""
    if edition not in EDITIONS:
        raise StripeBillingError(f"Unknown edition: {edition!r}")

    async with _client() as client:
        prices = await _search(
            client,
            "prices",
            f"metadata['sae_edition']:'{edition}' AND active:'true'",
        )
        if not prices:
            await client.aclose()
            await ensure_products()
            return await create_checkout_session(
                edition,
                customer_email,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        price = prices[0]

        session = await _post(
            client,
            "/checkout/sessions",
            {
                "mode": "subscription",
                "line_items": [{"price": price["id"], "quantity": 1}],
                "customer_email": customer_email,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": {"sae_edition": edition},
                "subscription_data": {"metadata": {"sae_edition": edition}},
                "allow_promotion_codes": True,
            },
        )

    return {
        "checkout_url": session.get("url") or "",
        "session_id": session["id"],
    }


async def retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    """GET /checkout/sessions/:id — used by the success-page handler."""
    async with _client() as client:
        resp = await client.get(f"/checkout/sessions/{session_id}")
        if resp.status_code >= 400:
            raise StripeBillingError(
                f"Stripe session retrieve failed: {resp.status_code} {resp.text}"
            )
        return resp.json()


async def create_portal_session(
    stripe_customer_id: str,
    return_url: str = "https://app.saebooks.com.au/admin/license",
) -> dict[str, str]:
    """Create a Stripe Billing Portal session, return ``{portal_url}``."""
    async with _client() as client:
        resp = await client.post(
            "/billing_portal/sessions",
            data=_encode_form({"customer": stripe_customer_id, "return_url": return_url}),
        )
        if resp.status_code >= 400:
            raise StripeBillingError(
                f"Stripe portal session failed: {resp.status_code} {resp.text}"
            )
    return {"portal_url": resp.json().get("url") or ""}


__all__ = [
    "EDITIONS",
    "StripeBillingError",
    "StripeBillingNotConfigured",
    "create_checkout_session",
    "create_portal_session",
    "ensure_products",
    "retrieve_checkout_session",
]
