"""Phase 3c relay protocol unit tests — payload assembly + freshness.

Pure functions (no DB) over ``saebooks.services.ic_relay.protocol``:

* ``build_payload`` produces a canonical, deterministic dict with NO account
  ids, amount as a fixed-decimal string, issued_at as a UTC 'Z' timestamp;
* the built payload round-trips through ``signing.canonical_payload`` (so a
  sign/verify across the wire cannot drift);
* ``is_fresh`` accepts a recent message and rejects a stale OR far-future one
  (symmetric window — a bad clock can't widen the replay surface);
* ``parse_issued_at`` fails closed on garbage.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from saebooks.services.ic_relay import protocol, signing


def _ids() -> dict[str, uuid.UUID]:
    return {
        "ic_txn_id": uuid.uuid4(),
        "edge_id": uuid.uuid4(),
        "src_tenant_id": uuid.uuid4(),
        "dst_tenant_id": uuid.uuid4(),
        "nonce": uuid.uuid4(),
    }


def test_build_payload_has_no_account_ids_and_string_amount() -> None:
    ids = _ids()
    p = protocol.build_payload(
        amount=Decimal("5000"),
        entry_date=datetime(2026, 6, 6).date(),
        description="Director funds SAE working capital",
        issued_at=datetime(2026, 6, 6, 3, 14, tzinfo=UTC),
        **ids,
    )
    # No account ids on the wire.
    assert not any("account" in k for k in p), "payload must carry NO account ids"
    # Amount is a fixed 2dp string (not a float / Decimal).
    assert p["amount"] == "5000.00"
    assert isinstance(p["amount"], str)
    assert p["issued_at"] == "2026-06-06T03:14:00Z"
    assert p["v"] == protocol.PAYLOAD_VERSION


def test_built_payload_canonicalises_deterministically() -> None:
    ids = _ids()
    kw = dict(
        amount=Decimal("100.5"),
        entry_date=datetime(2026, 6, 6).date(),
        description="x",
        issued_at=datetime(2026, 6, 6, tzinfo=UTC),
        **ids,
    )
    a = protocol.build_payload(**kw)
    b = protocol.build_payload(**kw)
    # Same logical payload -> identical signed bytes (the whole point).
    assert signing.canonical_payload(a) == signing.canonical_payload(b)


def test_format_amount_rejects_non_positive() -> None:
    with pytest.raises(protocol.RelayPayloadError):
        protocol.format_amount(Decimal("0"))
    with pytest.raises(protocol.RelayPayloadError):
        protocol.format_amount(Decimal("-1"))


def test_is_fresh_accepts_recent_rejects_stale_and_future() -> None:
    now = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)
    window = 3600
    assert protocol.is_fresh(now - timedelta(seconds=10), window_seconds=window, now=now)
    # Stale (held then injected).
    assert not protocol.is_fresh(now - timedelta(seconds=7200), window_seconds=window, now=now)
    # Far-future (clock skew / forged) — symmetric window rejects it too.
    assert not protocol.is_fresh(now + timedelta(seconds=7200), window_seconds=window, now=now)


def test_parse_issued_at_round_trip_and_failure() -> None:
    dt = datetime(2026, 6, 6, 3, 14, tzinfo=UTC)
    raw = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert protocol.parse_issued_at(raw) == dt
    with pytest.raises(protocol.RelayPayloadError):
        protocol.parse_issued_at("not-a-timestamp")
