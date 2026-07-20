"""Stripe webhook handler.

Stripe's webhook contract:

1. Stripe POSTs a JSON event to a URL we give it. The body is the
   entire event object, unchanged.
2. Every request carries a ``Stripe-Signature`` header of the form
   ``t=<unix-ts>,v1=<hmac-sha256>``. We recompute the HMAC against
   the raw body + the configured endpoint secret and reject on
   mismatch. This is the ONLY authentication — the endpoint must be
   public (Stripe won't retry behind auth headers).
3. Events are delivered at-least-once; we must be idempotent on the
   event ID. Stripe sends exactly one event ID per logical event.

Scope: this module handles ``payment_intent.succeeded`` only — the
happy-path "customer paid, create a Payment row". Refunds, disputes,
subscription lifecycle etc. can land in later batches without
touching the signature-verification layer.

The Stripe SDK is NOT imported — the verification is ~20 lines of
stdlib hmac, and pulling a second SDK just for webhook-signature is
more surface area than the feature is worth.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.payment import (
    Payment,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from saebooks.money import money_quantum

logger = logging.getLogger("saebooks.stripe")

# Stripe's signature tolerance (seconds) — the default used by
# stripe-python. Prevents replay attacks older than ~5 min.
DEFAULT_TOLERANCE_SECONDS = 300


class StripeError(RuntimeError):
    """Base class for Stripe-layer errors."""


class StripeNotConfiguredError(StripeError):
    """Raised when the webhook secret isn't set."""


class StripeSignatureError(StripeError):
    """Raised when the Stripe-Signature header fails verification."""


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Parse 't=1234,v1=abcd,v1=efgh' -> (1234, ['abcd', 'efgh']).

    Stripe may send multiple ``v1=`` entries when a secret is being
    rotated; any match accepts the request.
    """
    ts: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                ts = int(value)
            except ValueError as exc:
                raise StripeSignatureError(
                    f"Invalid timestamp in Stripe-Signature: {value!r}"
                ) from exc
        elif key == "v1":
            signatures.append(value)
    if ts is None or not signatures:
        raise StripeSignatureError(
            "Stripe-Signature header missing 't=' or 'v1=' components"
        )
    return ts, signatures


def verify_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now_ts: int | None = None,
) -> None:
    """Raise :class:`StripeSignatureError` when the header doesn't match.

    Recomputes ``HMAC-SHA256(secret, f"{t}.{payload}")`` per Stripe's
    spec and checks against each ``v1=`` value in constant time.
    Rejects if the timestamp is older than ``tolerance_seconds``.
    """
    if not secret:
        raise StripeNotConfiguredError("Stripe webhook secret is empty")
    ts, signatures = _parse_signature_header(signature_header)

    current = now_ts if now_ts is not None else int(datetime.now().timestamp())
    if abs(current - ts) > tolerance_seconds:
        raise StripeSignatureError(
            f"Stripe-Signature timestamp off by "
            f"{abs(current - ts)}s (tolerance {tolerance_seconds}s)"
        )

    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    for sig in signatures:
        if hmac.compare_digest(expected, sig):
            return
    raise StripeSignatureError(
        "Stripe-Signature HMAC mismatch (secret may be wrong or "
        "payload was modified in transit)"
    )


def _cents_to_decimal(amount: int, currency: str) -> Decimal:
    """Stripe stores most currencies as integer cents; some (JPY) are whole."""
    # Zero-decimal currencies per Stripe docs. AUD is not one, so the
    # default divide-by-100 path is correct for our AU use case.
    zero_decimal = {"bif", "clp", "djf", "gnf", "jpy", "kmf",
                    "krw", "mga", "pyg", "rwf", "ugx", "vnd",
                    "vuv", "xaf", "xof", "xpf"}
    if currency.lower() in zero_decimal:
        return Decimal(amount)
    return (Decimal(amount) / Decimal(100)).quantize(money_quantum(2))


async def _find_contact(
    session: AsyncSession,
    company_id: uuid.UUID,
    payment_intent: Mapping[str, Any],
) -> Contact | None:
    """Best-effort contact resolution from a PaymentIntent.

    Looks at ``receipt_email``, falls back to ``customer`` metadata
    (unresolved — Stripe's customer_id isn't stored on our Contact).
    Returns ``None`` if no match — caller decides whether to fall back
    to a catch-all "Stripe Receipts" contact or drop the event.
    """
    email = payment_intent.get("receipt_email")
    if isinstance(email, str) and email:
        result = await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.email == email,
                Contact.archived_at.is_(None),
            )
        )
        hit = result.scalars().first()
        if hit is not None:
            return hit
    return None


async def handle_payment_intent_succeeded(
    session: AsyncSession,
    event: Mapping[str, Any],
    *,
    settings: Settings,
    company_id: uuid.UUID | None = None,
) -> Payment | None:
    """Create a ``Payment`` row from a ``payment_intent.succeeded`` event.

    Idempotent on the PaymentIntent ID — a second delivery of the same
    event returns the existing Payment row without creating a dup.

    Returns ``None`` when the integration can't create a Payment
    (missing default bank account, no matching contact, wrong event
    type) rather than raising — Stripe retries on non-2xx, and we want
    ack + log for "we saw it but can't act" cases. The caller's 2xx
    response tells Stripe not to retry.
    """
    etype = event.get("type")
    if etype != "payment_intent.succeeded":
        logger.debug("stripe: ignoring event type %s", etype)
        return None

    data = event.get("data") or {}
    intent = data.get("object") or {}
    intent_id = str(intent.get("id") or "")
    if not intent_id:
        logger.warning("stripe: event missing data.object.id; dropping")
        return None

    # Resolve company — explicit override wins, else fall back to the
    # only non-archived company on the instance (community edition has
    # exactly one).
    if company_id is None:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            logger.error("stripe: no active company found; dropping event")
            return None
        company_id = company.id

    if not settings.stripe_default_bank_account_id:
        logger.warning(
            "stripe: STRIPE_DEFAULT_BANK_ACCOUNT_ID unset; "
            "event %s ack'd but no Payment row created",
            intent_id,
        )
        return None
    bank_account_id = uuid.UUID(settings.stripe_default_bank_account_id)

    # Idempotency guard — match on the Stripe PaymentIntent id stashed
    # in the ``reference`` column.
    existing = await session.execute(
        select(Payment).where(
            Payment.company_id == company_id,
            Payment.reference == intent_id,
        )
    )
    dup = existing.scalars().first()
    if dup is not None:
        logger.info(
            "stripe: payment_intent %s already recorded as Payment %s",
            intent_id, dup.id,
        )
        return dup

    contact = await _find_contact(session, company_id, intent)
    if contact is None:
        logger.warning(
            "stripe: no contact matched receipt_email for intent %s; dropping",
            intent_id,
        )
        return None

    amount_int = int(intent.get("amount_received") or intent.get("amount") or 0)
    if amount_int <= 0:
        logger.warning("stripe: intent %s has zero amount; dropping", intent_id)
        return None

    currency = str(intent.get("currency") or "aud")
    amount = _cents_to_decimal(amount_int, currency)

    # Stripe timestamps are seconds-since-epoch.
    created_ts = int(intent.get("created") or 0) or int(
        datetime.now().timestamp()
    )
    payment_date = date.fromtimestamp(created_ts)

    pay = Payment(
        company_id=company_id,
        contact_id=contact.id,
        bank_account_id=bank_account_id,
        direction=PaymentDirection.INCOMING,
        method=PaymentMethod.CARD,
        status=PaymentStatus.DRAFT,
        payment_date=payment_date,
        amount=amount,
        reference=intent_id,
        notes=f"Stripe {currency.upper()} {amount_int}",
    )
    session.add(pay)
    await session.flush()
    logger.info(
        "stripe: created Payment %s from intent %s (contact=%s, amount=%s)",
        pay.id, intent_id, contact.id, amount,
    )
    return pay


def parse_event(payload: bytes) -> dict[str, Any]:
    """Decode and JSON-parse the raw body. Raises :class:`StripeError` on bad JSON."""
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StripeError(f"Stripe payload was not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise StripeError(f"Stripe payload was not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise StripeError(
            f"Stripe event was not a JSON object: {type(obj).__name__}"
        )
    return obj


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "StripeError",
    "StripeNotConfiguredError",
    "StripeSignatureError",
    "handle_payment_intent_succeeded",
    "parse_event",
    "verify_signature",
]
