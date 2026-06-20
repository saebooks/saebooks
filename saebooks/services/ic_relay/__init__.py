"""Intercompany REMOTE relay — cryptographic core (Phase 3a, SHIP-SAFE INERT).

This package holds the ship-safe foundation of the cross-DB intercompany relay:
the Ed25519 signing primitives (``signing``) and the per-edge key/token helpers
(``keys``). Nothing in the running app imports these yet — the dispatcher, the
broker, and the ``/ic/accept`` webhook that use them land in later phases. They
are built and unit-tested first so the wire protocol + key envelope are pinned
before any code relays a money-adjacent event. See
``~/.claude/plans/saebooks-remote-relay-plan.md`` §4.

Phase 3b/3c additions (this branch, ``feat/ic-remote-relay-live``)
-----------------------------------------------------------------
* ``protocol`` — single-source the canonical relay payload shape + freshness, so
  the originator (dispatcher) and receiver (``/ic/accept``) can never drift.
* ``broker_client`` — the originator's thin async client to the broker
  ``/ic/relay`` (mirrors ``bank_feeds.remote``).
* ``dispatcher`` — the lifespan task that drains ``ic_outbox`` and relays signed
  payloads; backs off and DEADs on failure, NEVER auto-reverses the local leg.
* ``enable`` — the authoriser edge-enable flow: dual-tenant grant (the 0156
  SECURITY DEFINER predicate, NO BYPASSRLS) + FIDO2 + per-side keypair wiring.

Decision record (baked per the plan's recommendation; flagged for Richard)
--------------------------------------------------------------------------
* **D1 = broker.** A money-free ``saebooks-group`` broker (own stack + DB) is the
  single key/endpoint registry + one audited delivery log; tenants publish ONE
  outbound URL. Cost: one hop + one stack. (vs direct tenant<->tenant, which
  smears key management + audit across every pair.)
* **D4 = flag default-OFF, per-stack go-live.** ``SAEBOOKS_IC_REMOTE_RELAY_ENABLED``
  defaults False. Off => the originator REMOTE post raises, the dispatcher task
  never starts, ``/ic/accept`` returns 503. The single most important
  reversibility lever. LOCAL intercompany is unaffected.
* **D5 = human-in-the-loop on a half-pair.** A DEAD ``ic_outbox`` row (max
  attempts exhausted) surfaces in the recon view (``recon``); it is NEVER
  auto-reversed — a delivery failure must not mutate already-final local books.

Invariant: NO BYPASSRLS data path. Each side writes only its OWN books under its
OWN FORCE-RLS, to its OWN edge-declared control + contra accounts. The wire
carries NO account ids. Trust = signature (Ed25519) + per-edge token, two
independent secrets both required.
"""
from __future__ import annotations

from saebooks.services.ic_relay import keys, signing

__all__ = ["keys", "signing"]
