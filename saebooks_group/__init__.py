"""saebooks-group — the intercompany relay BROKER (money-free).

A tiny, separate service that brokers cross-DB intercompany relay messages
between tenant stacks. Architectural hard rule (plan §2.1, brief): **the broker
NEVER holds money.** Its schema has NO ledger/GL tables (no accounts, no
journal_entries/lines, no ic_txn/ic_legs); its code imports NO saebooks posting
module (``saebooks.services.journal`` / ``intercompany``). It only:

* registers per-edge PAIRS (public keys + token *hashes* — never private keys,
  never token cleartext, never any account/amount it can act on);
* logs RELAY deliveries (routing metadata + a signature fingerprint);
* verifies the originator's signature and forwards the SAME signed envelope to
  the partner's ``/ic/accept``.

It reuses exactly ONE thing from the main tree: the crypto-pure
``saebooks.services.ic_relay.signing`` (Ed25519 verify + canonicalisation). That
module has no DB and no GL — importing it does not give the broker a money path.

Two CI assertions enforce the invariant (tests/test_broker_money_free.py):
  1. the broker schema contains none of the GL table names;
  2. no broker module imports the posting services.
"""
from __future__ import annotations

__all__: list[str] = []
