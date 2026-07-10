"""Intercompany REMOTE relay — cryptographic core (Phase 3a, SHIP-SAFE INERT).

This package holds the ship-safe foundation of the cross-DB intercompany relay:
the Ed25519 signing primitives (``signing``) and the per-edge key/token helpers
(``keys``). Nothing in the running app imports these yet — the dispatcher, the
broker, and the ``/ic/accept`` webhook that use them land in later phases. They
are built and unit-tested first so the wire protocol + key envelope are pinned
before any code relays a money-adjacent event. See
``~/.claude/plans/saebooks-remote-relay-plan.md`` §4.
"""
from __future__ import annotations

from saebooks.services.ic_relay import keys, signing

__all__ = ["keys", "signing"]
