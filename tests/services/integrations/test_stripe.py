"""Unit tests for saebooks.services.integrations.stripe.

Signature verification is pure (no HTTP, no DB). The
``handle_payment_intent_succeeded`` tests exercise the real DB via
AsyncSessionLocal to confirm the Payment row lands + idempotency works.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.payment import Payment, PaymentStatus
from saebooks.services.integrations.stripe import (
    DEFAULT_TOLERANCE_SECONDS,
    StripeNotConfiguredError,
    StripeSignatureError,
    _cents_to_decimal,
    handle_payment_intent_succeeded,
    parse_event,
    verify_signature,
)

SECRET = "whsec_testsecret"


def _sig(payload: bytes, ts: int, secret: str = SECRET) -> str:
    """Build a well-formed Stripe-Signature header."""
    msg = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_verify_rejects_empty_secret() -> None:
    with pytest.raises(StripeNotConfiguredError):
        verify_signature(b"{}", "t=1,v1=x", "")


def test_verify_rejects_missing_components() -> None:
    with pytest.raises(StripeSignatureError, match="missing"):
        verify_signature(b"{}", "v1=deadbeef", SECRET)


def test_verify_rejects_non_integer_ts() -> None:
    with pytest.raises(StripeSignatureError, match="timestamp"):
        verify_signature(b"{}", "t=notanint,v1=x", SECRET)


def test_verify_rejects_stale_timestamp() -> None:
    payload = b'{"hello": "world"}'
    # Timestamp way outside tolerance
    old_ts = 1_000
    header = _sig(payload, old_ts)
    with pytest.raises(StripeSignatureError, match="off by"):
        verify_signature(payload, header, SECRET, now_ts=1_000_000)


def test_verify_rejects_bad_hmac() -> None:
    payload = b'{"x":1}'
    ts = 1_700_000_000
    # Wrong secret
    header = _sig(payload, ts, secret="different-secret")
    with pytest.raises(StripeSignatureError, match="HMAC mismatch"):
        verify_signature(payload, header, SECRET, now_ts=ts)


def test_verify_accepts_valid_signature() -> None:
    payload = b'{"ok": true}'
    ts = 1_700_000_000
    header = _sig(payload, ts)
    # Returns None — no exception == pass
    verify_signature(payload, header, SECRET, now_ts=ts)


def test_verify_accepts_any_of_multiple_v1_signatures() -> None:
    """When Stripe rotates secrets it sends both old + new v1 sigs."""
    payload = b'{"ok": true}'
    ts = 1_700_000_000
    good_mac = hmac.new(
        SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    header = f"t={ts},v1=dead{'0' * 60},v1={good_mac}"
    verify_signature(payload, header, SECRET, now_ts=ts)


def test_default_tolerance_is_five_minutes() -> None:
    assert DEFAULT_TOLERANCE_SECONDS == 300


def test_cents_to_decimal_aud_divides_by_100() -> None:
    assert _cents_to_decimal(12345, "aud") == Decimal("123.45")


def test_cents_to_decimal_jpy_is_whole() -> None:
    # JPY is zero-decimal per Stripe — cents are actually yen.
    assert _cents_to_decimal(12345, "jpy") == Decimal("12345")


def test_parse_event_rejects_non_json() -> None:
    from saebooks.services.integrations.stripe import StripeError

    with pytest.raises(StripeError, match="valid JSON"):
        parse_event(b"<html>not json</html>")


def test_parse_event_rejects_json_array() -> None:
    from saebooks.services.integrations.stripe import StripeError

    with pytest.raises(StripeError, match="not a JSON object"):
        parse_event(b"[1, 2, 3]")


def test_parse_event_parses_valid_body() -> None:
    body = json.dumps({"type": "payment_intent.succeeded"}).encode()
    parsed = parse_event(body)
    assert parsed["type"] == "payment_intent.succeeded"


# ----- handle_payment_intent_succeeded integration tests ----- #


async def _company_ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Create a scratch company + contact + bank account.

    Returns (company_id, contact_id, bank_account_id, email).
    """
    async with AsyncSessionLocal() as session:
        # pick the seeded company
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        # find a bank account (1-1110 Cash at Bank in the AU seed)
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert bank is not None

        email = f"stripe-test-{uuid.uuid4().hex[:8]}@example.com"
        contact = Contact(
            company_id=company.id,
            name="Stripe Test Contact",
            contact_type=ContactType.CUSTOMER,
            email=email,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        return company.id, contact.id, bank.id, email


async def _cleanup_stripe_payments(contact_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        pays = (
            await session.execute(
                select(Payment).where(Payment.contact_id == contact_id)
            )
        ).scalars().all()
        for p in pays:
            await session.delete(p)
        contact = await session.get(Contact, contact_id)
        if contact is not None:
            await session.delete(contact)
        await session.commit()


def _event(
    intent_id: str = "pi_test123",
    amount: int = 10000,
    currency: str = "aud",
    receipt_email: str | None = None,
) -> dict:
    return {
        "id": "evt_test",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id,
                "amount": amount,
                "amount_received": amount,
                "currency": currency,
                "created": 1_700_000_000,
                "receipt_email": receipt_email,
            }
        },
    }


async def test_handle_returns_none_for_wrong_event_type() -> None:
    async with AsyncSessionLocal() as session:
        event = {"type": "invoice.paid", "data": {"object": {"id": "pi_1"}}}
        settings = Settings()  # type: ignore[call-arg]
        result = await handle_payment_intent_succeeded(
            session, event, settings=settings
        )
        assert result is None


async def test_handle_returns_none_without_default_bank_account() -> None:
    company_id, contact_id, _bank_id, email = await _company_ctx()
    try:
        async with AsyncSessionLocal() as session:
            settings = Settings(STRIPE_DEFAULT_BANK_ACCOUNT_ID="")  # type: ignore[call-arg,arg-type]
            result = await handle_payment_intent_succeeded(
                session,
                _event(receipt_email=email),
                settings=settings,
                company_id=company_id,
            )
            assert result is None
    finally:
        await _cleanup_stripe_payments(contact_id)


async def test_handle_creates_payment_on_happy_path() -> None:
    company_id, contact_id, bank_id, email = await _company_ctx()
    try:
        async with AsyncSessionLocal() as session:
            settings = Settings(  # type: ignore[call-arg]
                STRIPE_DEFAULT_BANK_ACCOUNT_ID=str(bank_id),  # type: ignore[arg-type]
            )
            pay = await handle_payment_intent_succeeded(
                session,
                _event(receipt_email=email, amount=15000),
                settings=settings,
                company_id=company_id,
            )
            assert pay is not None
            assert pay.amount == Decimal("150.00")
            assert pay.contact_id == contact_id
            assert pay.bank_account_id == bank_id
            assert pay.status == PaymentStatus.DRAFT
            assert pay.reference == "pi_test123"
            await session.commit()
    finally:
        await _cleanup_stripe_payments(contact_id)


async def test_handle_is_idempotent_on_intent_id() -> None:
    company_id, contact_id, bank_id, email = await _company_ctx()
    try:
        settings = Settings(  # type: ignore[call-arg]
            STRIPE_DEFAULT_BANK_ACCOUNT_ID=str(bank_id),  # type: ignore[arg-type]
        )
        async with AsyncSessionLocal() as session:
            pay1 = await handle_payment_intent_succeeded(
                session,
                _event(receipt_email=email),
                settings=settings,
                company_id=company_id,
            )
            assert pay1 is not None
            await session.commit()
            first_id = pay1.id
        async with AsyncSessionLocal() as session:
            pay2 = await handle_payment_intent_succeeded(
                session,
                _event(receipt_email=email),
                settings=settings,
                company_id=company_id,
            )
            assert pay2 is not None
            assert pay2.id == first_id  # same row returned
    finally:
        await _cleanup_stripe_payments(contact_id)


async def test_handle_returns_none_when_no_contact_matches_email() -> None:
    company_id, contact_id, bank_id, _email = await _company_ctx()
    try:
        async with AsyncSessionLocal() as session:
            settings = Settings(  # type: ignore[call-arg]
                STRIPE_DEFAULT_BANK_ACCOUNT_ID=str(bank_id),  # type: ignore[arg-type]
            )
            result = await handle_payment_intent_succeeded(
                session,
                _event(receipt_email="not-in-db@example.com"),
                settings=settings,
                company_id=company_id,
            )
            assert result is None
    finally:
        await _cleanup_stripe_payments(contact_id)
