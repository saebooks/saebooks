"""Canonical relay-payload assembly + freshness for the IC REMOTE relay.

SHIP-SAFE seam (Phase 3c). The wire body is defined ONCE here so the
originator (dispatcher) and the receiver (``/ic/accept``) cannot drift —
a sign/verify mismatch from a divergent payload shape is the classic
silent failure. ``signing.canonical_payload`` then turns this dict into the
exact deterministic bytes that get Ed25519-signed.

The payload deliberately carries NO account ids (plan §4.3): each side posts
to its OWN edge-declared control account, so the partner can never direct a
posting into an arbitrary account of ours. Amounts are fixed-decimal *strings*
(``signing.canonical_payload`` refuses a float).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

PAYLOAD_VERSION = 1

# Direction labels carried in the body (mirror IcLegSide semantics on the wire).
DIR_SRC_TO_DST = "SRC_TO_DST"
DIR_DST_TO_SRC = "DST_TO_SRC"


class RelayPayloadError(ValueError):
    """Raised when a relay payload is structurally invalid (missing/typed)."""


def format_amount(amount: Decimal) -> str:
    """Return the fixed two-decimal string form used on the wire.

    Quantising to 2dp here (not at the call sites) keeps the canonical form
    single-sourced. A negative or zero amount is refused — a relay event is a
    positive economic movement; sign/direction is carried by ``direction``.
    """
    if amount <= Decimal("0"):
        raise RelayPayloadError("relay amount must be positive")
    return f"{amount.quantize(Decimal('0.01'))}"


def build_payload(
    *,
    ic_txn_id: uuid.UUID,
    edge_id: uuid.UUID,
    src_tenant_id: uuid.UUID,
    dst_tenant_id: uuid.UUID,
    amount: Decimal,
    entry_date: date,
    description: str | None,
    nonce: uuid.UUID,
    issued_at: datetime | None = None,
    direction: str = DIR_SRC_TO_DST,
) -> dict[str, Any]:
    """Assemble the canonical relay body (the dict signing canonicalises).

    Every value is a JSON-native, deterministic-friendly type: uuids/dates as
    ISO strings, amount as a fixed-decimal string, issued_at as a UTC
    ISO-8601 'Z' timestamp. ``description`` is passed through verbatim
    (``canonical_payload`` uses ``ensure_ascii=False`` so unicode round-trips).
    """
    when = issued_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return {
        "v": PAYLOAD_VERSION,
        "ic_txn_id": str(ic_txn_id),
        "edge_id": str(edge_id),
        "src_tenant_id": str(src_tenant_id),
        "dst_tenant_id": str(dst_tenant_id),
        "direction": direction,
        "amount": format_amount(amount),
        "entry_date": entry_date.isoformat(),
        "description": description or "",
        "nonce": str(nonce),
        "issued_at": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def parse_issued_at(raw: str) -> datetime:
    """Parse the wire ``issued_at`` back to an aware UTC datetime.

    Raises ``RelayPayloadError`` on a malformed value (fail closed — the caller
    turns this into a 400, never trusts a bad timestamp).
    """
    try:
        # Accept the 'Z' form we emit plus any RFC-3339 offset.
        norm = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(norm)
    except (ValueError, TypeError) as exc:
        raise RelayPayloadError(f"malformed issued_at: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_fresh(issued_at: datetime, *, window_seconds: int, now: datetime | None = None) -> bool:
    """True iff ``issued_at`` is within ``window_seconds`` of ``now`` (UTC).

    Rejects BOTH a too-old message (held then injected) AND a far-future one
    (clock-skew / forged) — the window is symmetric so a bad clock can't widen
    the replay surface. ``now`` is injectable for deterministic tests.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = abs((current - issued_at).total_seconds())
    return delta <= window_seconds
