"""Phase 3a signing + key/token unit tests (backend-agnostic).

Covers ``saebooks.services.ic_relay.signing`` and ``...keys``:

* canonicalisation is deterministic (key order independent, no whitespace) and
  refuses non-string money fields;
* Ed25519 sign/verify round-trips; a tampered body fails verify; ``verify``
  never raises on garbage (fails closed);
* the per-edge private key Fernet-wraps + unwraps to the same bytes;
* the per-edge token issues with a prefix and bcrypt-verifies; a wrong token
  fails; a malformed hash fails closed.

These are pure-crypto tests — no DB, no RLS — so they run on every backend.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SAEBOOKS_ENV", "test")
# A valid Fernet key for the wrap/unwrap round-trip. This matches the CI
# migrations job key shape (base64, 32 bytes).
os.environ.setdefault(
    "SAEBOOKS_FIELD_ENCRYPTION_KEY",
    "c2FlYm9va3MtdGVzdC1rZXktZG8tbm90LXVzZS1wcm8=",
)

from saebooks.services.ic_relay import keys, signing

# --------------------------------------------------------------------------- #
# canonical_payload
# --------------------------------------------------------------------------- #


def _payload(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "v": 1,
        "ic_txn_id": "11111111-1111-1111-1111-111111111111",
        "edge_id": "22222222-2222-2222-2222-222222222222",
        "src_tenant_id": "33333333-3333-3333-3333-333333333333",
        "dst_tenant_id": "44444444-4444-4444-4444-444444444444",
        "direction": "SRC_TO_DST",
        "amount": "5000.00",
        "entry_date": "2026-06-06",
        "description": "Director funds SAE working capital",
        "nonce": "55555555-5555-5555-5555-555555555555",
        "issued_at": "2026-06-06T03:14:00Z",
    }
    base.update(over)
    return base


def test_canonical_is_key_order_independent() -> None:
    a = {"b": "2", "a": "1", "amount": "10.00"}
    b = {"amount": "10.00", "a": "1", "b": "2"}
    assert signing.canonical_payload(a) == signing.canonical_payload(b)


def test_canonical_has_no_insignificant_whitespace() -> None:
    out = signing.canonical_payload({"a": "1", "amount": "10.00"})
    assert b", " not in out and b": " not in out
    assert out == b'{"a":"1","amount":"10.00"}'


def test_canonical_unicode_description_round_trips_bytes() -> None:
    # ensure_ascii=False keeps the unicode stable across encoders.
    out = signing.canonical_payload({"description": "café — naïve"})
    assert "café — naïve".encode() in out


def test_canonical_rejects_non_string_amount() -> None:
    with pytest.raises(signing.CanonicalisationError):
        signing.canonical_payload(_payload(amount=5000.00))  # float, not str


def test_canonical_rejects_nan() -> None:
    with pytest.raises(signing.CanonicalisationError):
        signing.canonical_payload({"x": float("nan")})


# --------------------------------------------------------------------------- #
# sign / verify
# --------------------------------------------------------------------------- #


def test_sign_verify_round_trip() -> None:
    priv, pub = signing.generate_keypair()
    body = signing.canonical_payload(_payload())
    sig = signing.sign(body, priv)
    assert signing.verify(body, sig, pub) is True


def test_verify_false_on_tampered_body() -> None:
    priv, pub = signing.generate_keypair()
    sig = signing.sign(signing.canonical_payload(_payload()), priv)
    tampered = signing.canonical_payload(_payload(amount="9999.00"))
    assert signing.verify(tampered, sig, pub) is False


def test_verify_false_on_wrong_key() -> None:
    priv, _ = signing.generate_keypair()
    _, other_pub = signing.generate_keypair()
    body = signing.canonical_payload(_payload())
    sig = signing.sign(body, priv)
    assert signing.verify(body, sig, other_pub) is False


def test_verify_never_raises_on_garbage() -> None:
    # Malformed signature + malformed key must return False, not raise.
    assert signing.verify(b"body", b"not-a-sig", b"not-a-key") is False
    assert signing.verify(b"body", b"", b"") is False


def test_public_key_for_matches_generated_pair() -> None:
    priv, pub = signing.generate_keypair()
    assert signing.public_key_for(priv) == pub


# --------------------------------------------------------------------------- #
# key wrap / unwrap
# --------------------------------------------------------------------------- #


def test_private_key_wrap_unwrap_round_trip() -> None:
    priv, _ = keys.new_signing_key()
    ct = keys.wrap_private_key(priv)
    assert ct != ""
    assert priv not in ct.encode("utf-8")  # not stored in cleartext
    assert keys.unwrap_private_key(ct) == priv


def test_wrapped_key_still_signs() -> None:
    priv, pub = keys.new_signing_key()
    recovered = keys.unwrap_private_key(keys.wrap_private_key(priv))
    body = signing.canonical_payload(_payload())
    assert signing.verify(body, signing.sign(body, recovered), pub) is True


# --------------------------------------------------------------------------- #
# per-edge token
# --------------------------------------------------------------------------- #


def test_edge_token_issue_and_verify() -> None:
    cleartext, prefix = keys.generate_edge_token()
    assert cleartext.startswith("icrl_")
    assert keys.token_lookup_prefix(cleartext) == prefix
    h = keys.hash_edge_token(cleartext)
    assert keys.verify_edge_token(cleartext, h) is True


def test_edge_token_wrong_cleartext_fails() -> None:
    cleartext, _ = keys.generate_edge_token()
    other, _ = keys.generate_edge_token()
    h = keys.hash_edge_token(cleartext)
    assert keys.verify_edge_token(other, h) is False


def test_edge_token_malformed_hash_fails_closed() -> None:
    assert keys.verify_edge_token("icrl_whatever", "not-a-bcrypt-hash") is False


def test_token_lookup_prefix_empty_for_foreign_header() -> None:
    assert keys.token_lookup_prefix("saebk_deadbeef") == ""
