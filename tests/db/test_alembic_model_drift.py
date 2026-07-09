"""E3 — alembic model-vs-schema drift guard.

The 0142 incident (an ORM column shipped without a matching migration ->
~200 test failures on a fresh DB) had no automated guard. This test runs
``compare_metadata`` between the migrated test DB (the harness runs
``alembic upgrade head`` before pytest) and the ORM ``Base.metadata``, and
FAILS if the ORM declares any table or column that no migration created.

Why scope to ``add_table`` / ``add_column`` only
-------------------------------------------------
``compare_metadata`` is bidirectional. Against this codebase's clean head
it emits ~300 diffs, but they fall into two buckets:

  * DB-ahead-of-ORM (benign here): ``remove_table`` / ``remove_index`` /
    ``remove_column`` / ``remove_fk`` / ``modify_comment`` / ``add_index``.
    These are migration-only artefacts the ORM never models — raw JSONB
    reference tables (``raw_au_*``), migration-declared indexes/comments,
    payroll/TPAR/super tables whose ORM models aren't imported into
    ``Base.metadata`` at request time, etc. None of them break inserts.

  * ORM-ahead-of-DB (the 0142 bug class): ``add_table`` / ``add_column``.
    These mean the ORM maps a table/column that ``alembic upgrade head``
    never created — so every INSERT/SELECT touching it explodes at
    runtime. This is precisely what shipped in 0142.

So the guard fails ONLY on ``add_table`` + ``add_column``. Verified
non-vacuous: a simulated 0142 (adding an un-migrated ORM column) makes
this test go red; against the real clean head it is green because there
are zero such ops. The benign buckets are intentionally NOT asserted on
— folding them in would make the test a 300-line allowlist that rots and
provides no extra safety against the actual failure mode.
"""
from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

import saebooks.models  # noqa: F401  (register every ORM table on Base.metadata)
from saebooks.db import Base, engine

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

# Operations that indicate the ORM declares schema the migrations never
# created — the 0142 failure class. These are the ONLY drift ops that
# break runtime inserts/queries, so they are the ONLY ones we fail on.
_FORBIDDEN_OPS = frozenset({"add_table", "add_column"})

# Schemas OTHER than the connection default (public) where migrations
# legitimately relocate ORM-mapped tables. The ORM models stay
# schema-agnostic (``schema=None``) so the Cashbook SQLite backend keeps
# working, but migration 0172 moves the pre-accounting tables to
# ``preaccounting`` and 0173 moves the capture (bank-feed) tables to
# ``capture`` via ``ALTER TABLE ... SET SCHEMA``. ``compare_metadata``
# only reflects the default schema, so it reports those relocated tables
# as ``add_table`` (ORM has them, ``public`` no longer does). That is NOT
# the 0142 bug class — the table DOES exist, just in another schema. We
# resolve it by checking whether a same-named table lives in one of these
# non-default schemas and, if so, excluding the op. A genuinely
# un-migrated ORM table (absent from public AND from ALL these schemas)
# still fails the guard, so this teaches the test without weakening it.
_RELOCATED_SCHEMAS: tuple[str, ...] = ("preaccounting", "capture")


def _op_table_name(op: tuple) -> str | None:
    """Return the table name an add_table/add_column op refers to."""
    kind = op[0]
    if kind == "add_table":
        return op[1].name
    if kind == "add_column":
        # ('add_column', schema, table_name, Column)
        return op[2]
    return None


def _describe(op: tuple) -> str:
    """Human-readable one-liner for a forbidden op tuple."""
    kind = op[0]
    if kind == "add_table":
        return f"add_table: {op[1].name}"
    if kind == "add_column":
        # ('add_column', schema, table_name, Column)
        return f"add_column: {op[2]}.{op[3].name}"
    return repr(op)[:200]


async def test_no_orm_schema_drift_vs_migrations() -> None:
    """No ORM table/column may lack a migration (alembic autogen drift)."""

    def _diff(sync_conn):
        # Mirror alembic/env.py's context config (compare_type=True) so
        # the comparison matches what `alembic revision --autogenerate`
        # would itself produce.
        mc = MigrationContext.configure(
            sync_conn,
            opts={"compare_type": True, "target_metadata": Base.metadata},
        )
        return compare_metadata(mc, Base.metadata)

    async with engine.connect() as conn:
        diffs = await conn.run_sync(_diff)
        # Tables the migrations legitimately relocated to a non-default
        # schema (0172 → preaccounting). Names here satisfy an ORM
        # ``add_table``/``add_column`` op even though ``public`` no
        # longer holds them.
        relocated_rows = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = ANY(:schemas)"
                ),
                {"schemas": list(_RELOCATED_SCHEMAS)},
            )
        ).all()
    relocated_tables = {r.tablename for r in relocated_rows}

    # compare_metadata returns a flat list whose entries are either a
    # single op-tuple (table-level) or a LIST of op-tuples (column-level,
    # grouped per table). Flatten before classifying.
    flat: list[tuple] = []
    for entry in diffs:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)

    offending = [
        op
        for op in flat
        if op
        and op[0] in _FORBIDDEN_OPS
        and _op_table_name(op) not in relocated_tables
    ]

    assert not offending, (
        "ORM model has tables/columns with no matching migration "
        "(0142-class drift). Generate a migration for:\n  - "
        + "\n  - ".join(_describe(op) for op in offending)
    )
