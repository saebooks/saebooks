"""Stripe outbound integration for the public-tier checkout flow.

Implemented against Stripe's REST API directly via ``httpx`` — we
avoid the Stripe Python SDK because it's heavy (~MB), introduces a
new global state pattern, and we only need three endpoints (products,
prices, checkout sessions). The existing webhook signature
verification in ``services/integrations/stripe.py`` is also stdlib-only.

Two responsibilities:

1. ``ensure_products()`` — idempotently creates the Business and Pro
   recurring prices in Stripe, one price per (edition, billing period).
   Searches by metadata sentinel ``sae_edition`` for the product, and
   matches on ``sae_edition`` + ``sae_period`` for the price, so
   re-running the script doesn't double-up.
2. ``create_checkout_session(edition, customer_email, *, period)`` —
   builds a Stripe Checkout Session for the requested
   (edition, period) tuple and returns the hosted-checkout URL.

Live/test guard
---------------
``STRIPE_LIVE != "1"`` requires the secret key to start with ``sk_test_``,
otherwise we abort. Protects an instance that accidentally booted with
a live key.

Pricing
-------
AUD; cents in ``EDITIONS[edition]["prices"][period]``. Display strings
match the marketing site ($49/mo, $490/yr, $99/mo, $990/yr — yearly is
exactly 10x monthly, "save 2 months").
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal
from urllib.parse import urlencode as _urlencode

import httpx

from saebooks.config import settings

logger = logging.getLogger("saebooks.stripe_billing")

Edition = Literal["business", "pro"]
Period = Literal["month", "year"]

# Per-edition spec. ``prices`` maps the Stripe ``recurring.interval`` value
# to ``unit_amount`` (cents in the edition's ``currency``). Yearly is
# exactly 10x monthly — honest "save 2 months" framing on the site.
EDITIONS: dict[str, dict[str, Any]] = {
    "business": {
        "name": "SAE Books Business",
        "description": "Business edition (single company, up to 3 users).",
        "currency": "aud",
        "prices": {
            "month": 4900,    # $49/mo
            "year": 49000,    # $490/yr
        },
    },
    "pro": {
        "name": "SAE Books Pro",
        "description": "Pro edition (unlimited users, multi-company, STP, FX).",
        "currency": "aud",
        "prices": {
            "month": 9900,    # $99/mo
            "year": 99000,    # $990/yr
        },
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
    # NOTE: pass body as pre-encoded bytes via content= rather than
    # data=. httpx 0.28.x has a regression where data= form bodies
    # combined with HTTPBasic auth on certain endpoints (incl. all
    # Stripe POSTs) yield RuntimeError: Attempted to send an sync
    # request with an AsyncClient instance. Pre-encoding sidesteps
    # the buggy form-stream path. See:
    # https://github.com/encode/httpx/issues/3079 (similar)
    body = _urlencode(_encode_form(payload)).encode("utf-8")
    resp = await client.post(
        path,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code >= 400:
        logger.error("stripe_billing: POST %s failed: %s", path, resp.text)
        resp.raise_for_status()
    return resp.json()


async def ensure_products() -> dict[str, dict[str, str]]:
    """Idempotently create products + recurring prices for each
    edition × billing period. Returns ``{edition: {period: price_id}}``.

    Search uses metadata ``sae_edition`` (product) and
    ``sae_edition`` + ``sae_period`` (price). Re-runs read existing
    rows back instead of creating duplicates.

    Existing live monthly prices that pre-date the ``sae_period``
    metadata field are matched by amount/currency/interval and
    backfilled with ``metadata.sae_period`` so subsequent runs find
    them via the metadata search path.
    """
    out: dict[str, dict[str, str]] = {}
    async with _client() as client:
        for edition, spec in EDITIONS.items():
            # ----- product (one per edition) -----
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

            # ----- prices (one per period) -----
            edition_prices: dict[str, str] = {}
            existing_prices = await _list(
                client, "prices", product=product["id"], active="true", limit=100
            )

            for period, amount_cents in spec["prices"].items():
                match: dict[str, Any] | None = None
                for p in existing_prices:
                    recurring = p.get("recurring") or {}
                    md = p.get("metadata") or {}
                    same_money = (
                        p.get("unit_amount") == amount_cents
                        and (p.get("currency") or "").lower() == spec["currency"]
                        and recurring.get("interval") == period
                    )
                    md_period = md.get("sae_period")
                    md_edition = md.get("sae_edition")
                    # Two acceptance modes:
                    #   1. Metadata matches outright (post-period prices).
                    #   2. Money matches and metadata is missing/legacy
                    #      (pre-period live monthly prices). We backfill
                    #      metadata so future runs find them by metadata.
                    if md_period == period and md_edition in (edition, None):
                        if same_money:
                            match = p
                            break
                    elif md_period is None and same_money:
                        # Legacy match — backfill metadata.
                        logger.info(
                            "stripe_billing: backfilling sae_period=%s on legacy price %s",
                            period,
                            p["id"],
                        )
                        await _post(
                            client,
                            f"/prices/{p['id']}",
                            {
                                "metadata": {
                                    "sae_edition": edition,
                                    "sae_period": period,
                                },
                            },
                        )
                        match = p
                        break

                if match is None:
                    match = await _post(
                        client,
                        "/prices",
                        {
                            "product": product["id"],
                            "currency": spec["currency"],
                            "unit_amount": amount_cents,
                            "recurring": {"interval": period},
                            "metadata": {
                                "sae_edition": edition,
                                "sae_period": period,
                            },
                        },
                    )
                    logger.info(
                        "stripe_billing: created price %s/%s (id=%s)",
                        edition,
                        period,
                        match["id"],
                    )
                edition_prices[period] = match["id"]
            out[edition] = edition_prices
    return out


# ---------------------------------------------------------------------------
# Checkout session creation
# ---------------------------------------------------------------------------


async def create_checkout_session(
    edition: Edition,
    customer_email: str,
    *,
    period: Period = "month",
    success_url: str = "https://app.saebooks.com.au/billing/checkout-success?session_id={CHECKOUT_SESSION_ID}",
    cancel_url: str = "https://saebooks.com.au/#editions",
) -> dict[str, str]:
    """Create a Stripe Checkout Session for ``(edition, period)``.

    Returns ``{checkout_url, session_id}``. Stamps both
    ``sae_edition`` and ``sae_period`` on the session metadata and
    on subscription_data so the webhook (or any downstream consumer)
    can tell month from year — currently only ``sae_edition`` drives
    tenant.edition state, but future per-period logic (e.g. billing
    portal copy, license expiry hints) can read ``sae_period``.
    """
    if edition not in EDITIONS:
        raise StripeBillingError(f"Unknown edition: {edition!r}")
    if period not in ("month", "year"):
        raise StripeBillingError(f"Unknown period: {period!r}")

    async with _client() as client:
        prices = await _search(
            client,
            "prices",
            (
                f"metadata['sae_edition']:'{edition}' "
                f"AND metadata['sae_period']:'{period}' "
                f"AND active:'true'"
            ),
        )
        if not prices:
            # Materialise products/prices and retry once. ensure_products()
            # also backfills sae_period on legacy live prices.
            await ensure_products()
            prices = await _search(
                client,
                "prices",
                (
                    f"metadata['sae_edition']:'{edition}' "
                    f"AND metadata['sae_period']:'{period}' "
                    f"AND active:'true'"
                ),
            )
            if not prices:
                raise StripeBillingError(
                    f"No active Stripe price for {edition}/{period} after ensure_products()"
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
                "metadata": {"sae_edition": edition, "sae_period": period},
                "subscription_data": {
                    "metadata": {"sae_edition": edition, "sae_period": period},
                },
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
        # See _post note: httpx 0.28 + tuple-auth + data= breaks; use content=.
        _body = _urlencode(_encode_form({"customer": stripe_customer_id, "return_url": return_url})).encode("utf-8")
        resp = await client.post(
            "/billing_portal/sessions",
            content=_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise StripeBillingError(
                f"Stripe portal session failed: {resp.status_code} {resp.text}"
            )
    return {"portal_url": resp.json().get("url") or ""}


__all__ = [
    "EDITIONS",
    "Edition",
    "Period",
    "StripeBillingError",
    "StripeBillingNotConfigured",
    "create_checkout_session",
    "create_portal_session",
    "ensure_products",
    "retrieve_checkout_session",
]
