"""Stripe Checkout Session creation — outbound payment link generation (B/48).

Creates a hosted Stripe Checkout Session for a posted invoice and returns
the ``url`` from the response.  The session is ``mode=payment`` with one
line item per invoice line.

The Stripe Python SDK is NOT imported — the API surface we need is a
single POST, and the codebase already uses httpx directly for all other
integrations (LEI, Companies House, SISS, ABR, Paperless).  Pulling the
SDK just for this call would add ~10 MB of transitive deps.

Configuration
-------------
``STRIPE_SECRET_KEY`` — required.  When empty, ``create_payment_link``
raises ``StripeNotConfiguredError`` immediately (before any network call).

Stripe API reference
--------------------
POST https://api.stripe.com/v1/checkout/sessions
  Authorization: Basic base64(sk_…:)   (empty password, per Stripe docs)
  Content-Type: application/x-www-form-urlencoded

The Checkout Sessions API uses form-encoded bodies, *not* JSON.
"""
from __future__ import annotations

import base64
import logging
from decimal import Decimal
from typing import Any

import httpx

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.services.integrations.stripe import StripeError, StripeNotConfiguredError

logger = logging.getLogger("saebooks.stripe_links")

_CHECKOUT_URL = "https://api.stripe.com/v1/checkout/sessions"

# Stripe zero-decimal currencies (same list as the webhook handler).
_ZERO_DECIMAL: frozenset[str] = frozenset({
    "bif", "clp", "djf", "gnf", "jpy", "kmf",
    "krw", "mga", "pyg", "rwf", "ugx", "vnd",
    "vuv", "xaf", "xof", "xpf",
})


def _to_cents(amount: Decimal, currency: str) -> int:
    """Convert a decimal amount to the integer Stripe expects.

    Most currencies: multiply by 100.  Zero-decimal currencies (JPY, etc.)
    pass the whole-unit amount unchanged.
    """
    if currency.lower() in _ZERO_DECIMAL:
        return int(amount.to_integral_value())
    return int((amount * 100).to_integral_value())


def _build_form_data(
    invoice: dict[str, Any],
    *,
    success_url: str,
    cancel_url: str,
) -> dict[str, str]:
    """Build the form-encoded key=value dict for the Checkout Sessions API.

    Stripe's API uses PHP-array-style notation for nested / repeated fields:
    ``line_items[0][price_data][currency]``.

    ``invoice`` must contain at minimum:
    * ``id`` — UUID string, stored in ``metadata[invoice_id]``
    * ``currency`` — ISO 4217 (e.g. "AUD")
    * ``lines`` — list of dicts with ``description`` and ``line_total``
    * ``total`` — Decimal (fallback when lines is empty)

    At least one line item is always produced.  If ``lines`` is empty we
    synthesise a single line from ``invoice["total"]`` labelled "Invoice
    <number or id>".
    """
    currency = str(invoice.get("currency") or "AUD").lower()
    lines = invoice.get("lines") or []
    invoice_id = str(invoice.get("id") or "")
    invoice_label = str(invoice.get("number") or invoice_id or "Invoice")

    data: dict[str, str] = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[invoice_id]": invoice_id,
    }

    if lines:
        for idx, line in enumerate(lines):
            description = str(line.get("description") or f"Line {idx + 1}")
            # Use line_total (subtotal + tax) as the Stripe unit_amount so
            # the customer sees the GST-inclusive price.
            amount = Decimal(str(line.get("line_total") or 0))
            cents = _to_cents(amount, currency)
            prefix = f"line_items[{idx}]"
            data[f"{prefix}[quantity]"] = "1"
            data[f"{prefix}[price_data][currency]"] = currency
            data[f"{prefix}[price_data][unit_amount]"] = str(cents)
            data[f"{prefix}[price_data][product_data][name]"] = description[:500]
    else:
        # Fallback — synthesise one line from the invoice total.
        total = Decimal(str(invoice.get("total") or 0))
        cents = _to_cents(total, currency)
        data["line_items[0][quantity]"] = "1"
        data["line_items[0][price_data][currency]"] = currency
        data["line_items[0][price_data][unit_amount]"] = str(cents)
        data[f"line_items[0][price_data][product_data][name]"] = (
            f"Invoice {invoice_label}"
        )

    return data


def _auth_header(secret_key: str) -> str:
    """Return the Basic auth header value for the given Stripe secret key.

    Stripe uses HTTP Basic auth with the API key as the username and an
    empty string as the password.
    """
    token = base64.b64encode(f"{secret_key}:".encode()).decode()
    return f"Basic {token}"


async def create_payment_link(
    invoice: dict[str, Any],
    *,
    success_url: str = "",
    cancel_url: str = "",
    settings: Settings | None = None,
    _client: httpx.AsyncClient | None = None,
) -> str:
    """Create a Stripe Checkout Session and return the hosted payment URL.

    Parameters
    ----------
    invoice:
        Invoice dict as returned by ``InvoiceOut.model_dump()`` or the
        equivalent mapping.  Required keys: ``id``, ``currency``, ``total``;
        ``lines`` is used when present.
    success_url:
        URL the customer is redirected to after a successful payment.
        Defaults to ``/invoices/{invoice_id}?stripe=success``.
    cancel_url:
        URL the customer is redirected to when they abandon the checkout.
        Defaults to ``/invoices/{invoice_id}?stripe=cancel``.
    settings:
        Override the module-level settings singleton (useful in tests).
    _client:
        Inject an ``httpx.AsyncClient`` for tests (respx mock transport).

    Returns
    -------
    str
        The Stripe Checkout Session ``url`` — a ``https://checkout.stripe.com/…``
        redirect.

    Raises
    ------
    StripeNotConfiguredError
        When ``STRIPE_SECRET_KEY`` is empty.
    StripeError
        When the Stripe API returns a non-2xx response.
    """
    cfg = settings if settings is not None else _default_settings
    if not cfg.stripe_secret_key:
        raise StripeNotConfiguredError(
            "STRIPE_SECRET_KEY is not configured; cannot create a payment link"
        )

    invoice_id = str(invoice.get("id") or "")
    if not success_url:
        success_url = f"/invoices/{invoice_id}?stripe=success"
    if not cancel_url:
        cancel_url = f"/invoices/{invoice_id}?stripe=cancel"

    form_data = _build_form_data(invoice, success_url=success_url, cancel_url=cancel_url)

    headers = {
        "Authorization": _auth_header(cfg.stripe_secret_key),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async def _post(client: httpx.AsyncClient) -> str:
        response = await client.post(_CHECKOUT_URL, data=form_data, headers=headers)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise StripeError(
                f"Stripe API error {response.status_code}: {detail}"
            )
        body = response.json()
        url = body.get("url")
        if not url:
            raise StripeError(
                "Stripe Checkout Session response missing 'url' field"
            )
        logger.info(
            "stripe: created checkout session %s for invoice %s",
            body.get("id"),
            invoice_id,
        )
        return str(url)

    if _client is not None:
        return await _post(_client)

    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _post(client)


__all__ = [
    "create_payment_link",
]
