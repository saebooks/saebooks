"""The broker NEVER holds money — CI assertion (Phase 3b invariant).

Two independent checks enforce plan §2.1 / the build brief:

1. **Schema check** — the broker's declarative metadata (saebooks_group) contains
   ONLY ``pair_registry`` + ``relay_log`` and NONE of the GL/ledger table names.
   A future migration that added an ``accounts`` / ``journal_entries`` table to
   the broker would fail this and block the build.

2. **Import-graph check** — the broker app's transitive import graph imports NO
   posting service (``saebooks.services.journal`` / ``saebooks.services.intercompany``)
   and NONE of the GL ORM models. The broker reuses only the crypto-pure
   ``saebooks.services.ic_relay.signing`` (Ed25519 verify), which has no GL.

Backend-agnostic (no DB, no RLS) — pure introspection, runs on every backend.
"""
from __future__ import annotations

import sys

# The GL / money table names that must NEVER appear in the broker schema.
_FORBIDDEN_TABLES = {
    "accounts",
    "journal_entries",
    "journal_lines",
    "ic_txn",
    "ic_legs",
    "ic_outbox",
    "ic_inbox",
    "invoices",
    "bills",
    "payments",
}

# Posting modules the broker must never import (transitively).
_FORBIDDEN_MODULE_SUBSTRINGS = (
    "saebooks.services.journal",
    "saebooks.services.intercompany",
    "saebooks.models.journal",
    "saebooks.models.account",
)


def test_broker_schema_has_no_gl_tables() -> None:
    # Import models so they register on Base.metadata before we inspect it.
    import saebooks_group.models  # noqa: F401
    from saebooks_group.db import Base

    tables = set(Base.metadata.tables.keys())
    assert tables == {"pair_registry", "relay_log"}, (
        f"broker schema must be exactly pair_registry + relay_log, got {tables}"
    )
    leaked = tables & _FORBIDDEN_TABLES
    assert not leaked, f"broker schema leaked GL/money tables: {leaked}"


def test_broker_app_imports_no_posting_code() -> None:
    """Import the broker app in a FRESH interpreter and assert no posting module.

    Critically this runs in a SUBPROCESS, not the test process: measuring the
    broker's import graph by mutating ``sys.modules`` in-process would corrupt
    the shared SQLAlchemy mapper registry (deleting ``saebooks.models.account``
    breaks every later test's ``accounts``->``companies`` FK resolution). A clean
    subprocess imports only ``saebooks_group.app`` and reports whether any
    forbidden posting/GL module entered its own ``sys.modules``.
    """
    import subprocess

    probe = (
        "import sys; import saebooks_group.app; "
        "subs = " + repr(list(_FORBIDDEN_MODULE_SUBSTRINGS)) + "; "
        "leaked = [n for n in sys.modules if any(s in n for s in subs)]; "
        "print('LEAKED:' + ','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"broker import probe failed: {result.stderr[-2000:]}"
    )
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("LEAKED:")),
        "LEAKED:",
    )
    leaked = [x for x in line[len("LEAKED:"):].split(",") if x]
    assert not leaked, (
        f"broker app transitively imported posting/GL code: {leaked} — the "
        f"money-free invariant is broken"
    )


def test_broker_models_are_only_pair_and_log() -> None:
    import saebooks_group.models as m

    # The module must define exactly the two table classes (plus enums).
    table_classes = [
        v
        for v in vars(m).values()
        if isinstance(v, type) and getattr(v, "__tablename__", None)
    ]
    names = {c.__tablename__ for c in table_classes}
    assert names == {"pair_registry", "relay_log"}, (
        f"broker models must define only pair_registry + relay_log, got {names}"
    )
