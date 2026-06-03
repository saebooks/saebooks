"""Strict-subset invariant: every (table, column, type) in cashbook.schema.sql
must exist in the live Postgres (SQLAlchemy ORM) schema with a compatible type.

This test is the CI gate for the Cashbook ↔ SAE Books upgrade/downgrade
feature. A failure here means either:

  (a) A column was added to a Cashbook SQLite table that has no counterpart in
      the Postgres schema — violating the strict-subset contract and breaking
      lossless upgrade.  Fix: add the column to the Postgres models first, or
      move it to a Cashbook-only table that has no Postgres equivalent.

  (b) The schema dump (db/cashbook.schema.sql) is stale.  Fix: re-run
      ``python scripts/dump_cashbook_schema.py`` and commit the result.

See ``[[cashbook-upgrade-downgrade-policy]]`` and
``[[saebooks-mobile-architecture]]`` in the project memory for the full
design rationale.

Type compatibility rules (narrowing allowed, widening fails):
  SQLite CHAR(32)    ↔  Postgres UUID
  SQLite JSON        ↔  Postgres JSONB or ARRAY(*)
  SQLite INTEGER     ↔  Postgres Integer / SmallInteger / BigInteger / Enum
  SQLite BIGINT      ↔  Postgres BigInteger
  SQLite SMALLINT    ↔  Postgres SmallInteger / Integer
  SQLite NUMERIC(p,s)↔  Postgres Numeric(p,s) — exact match required
  SQLite VARCHAR(n)  ↔  Postgres String(n) / String(≥n) / Text / Enum
  SQLite VARCHAR     ↔  Postgres String (no length) / Text / Enum
  SQLite TEXT        ↔  Postgres Text / String
  SQLite BOOLEAN     ↔  Postgres Boolean
  SQLite DATETIME    ↔  Postgres DateTime
  SQLite DATE        ↔  Postgres Date
  SQLite BLOB        ↔  Postgres LargeBinary
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path
from typing import NamedTuple

import pytest

import saebooks.models
from saebooks.db import Base

# ---------------------------------------------------------------------------
# Load the ORM metadata once at module import time.
# ---------------------------------------------------------------------------

for _mi in pkgutil.iter_modules(saebooks.models.__path__):
    importlib.import_module(f"saebooks.models.{_mi.name}")

# ---------------------------------------------------------------------------
# Locate cashbook.schema.sql relative to this file.
# tests/ lives one level below the repo root; db/ is at the repo root.
#
# The dev container mounts saebooks/, tests/, and alembic/ but NOT db/.
# We therefore keep a copy at tests/db/cashbook.schema.sql (inside the
# mounted tests/ tree) and prefer that when the repo-root copy is absent.
# Both paths are kept in sync by scripts/dump_cashbook_schema.py.
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
# Primary location: repo-root db/ (canonical, not mounted in dev container).
_SCHEMA_PATH_REPO = _REPO_ROOT / "db" / "cashbook.schema.sql"
# Fallback / container copy: inside tests/db/ (mounted in saebooks-dev-app-1).
_SCHEMA_PATH_TESTS = _TESTS_DIR / "db" / "cashbook.schema.sql"
# Resolve to whichever exists; prefer the tests-dir copy when inside a container.
_SCHEMA_PATH = _SCHEMA_PATH_TESTS if _SCHEMA_PATH_TESTS.exists() else _SCHEMA_PATH_REPO


# ---------------------------------------------------------------------------
# Schema parser
# ---------------------------------------------------------------------------

class _Column(NamedTuple):
    table: str
    name: str
    sqlite_type: str  # canonical upper-case token, e.g. "CHAR(32)", "JSON"


# Lines that are NOT column definitions inside a CREATE TABLE block.
_NON_COLUMN_RE = re.compile(
    r"^\s*(PRIMARY\s+KEY|CONSTRAINT|FOREIGN\s+KEY|UNIQUE|CHECK)\b",
    re.IGNORECASE,
)

# Extract the column name and raw type token from a DDL column line.
# Pattern: <whitespace><name> <TYPE_TOKEN> [rest...]
# The type token is the first "word" after the column name; it may include
# parenthesised args like NUMERIC(14, 2) or CHAR(32).
_COL_DEF_RE = re.compile(
    r"^\s+(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s+"
    r"(?P<type>[A-Z]+(?:\([^)]+\))?)",
    re.IGNORECASE,
)


def _parse_schema(sql_text: str) -> list[_Column]:
    """Return a list of _Column from a cashbook.schema.sql dump."""
    columns: list[_Column] = []
    current_table: str | None = None

    for raw_line in sql_text.splitlines():
        line = raw_line.rstrip()

        # New table block.
        m = re.match(r"^CREATE\s+TABLE\s+(\w+)\s*\(", line, re.IGNORECASE)
        if m:
            current_table = m.group(1)
            continue

        if current_table is None:
            continue

        # End of table block.
        if line.strip() == ");":
            current_table = None
            continue

        # Skip non-column lines (constraints, FKs, PKs).
        if _NON_COLUMN_RE.match(line):
            continue

        # Parse column definition.
        cm = _COL_DEF_RE.match(line)
        if cm:
            col_name = cm.group("name").lower()
            col_type = cm.group("type").upper()
            columns.append(_Column(current_table, col_name, col_type))

    return columns


# ---------------------------------------------------------------------------
# Type compatibility checker
# ---------------------------------------------------------------------------

def _postgres_type_name(col: object) -> str:
    """Return a short upper-case name for a SQLAlchemy column type."""
    from sqlalchemy import (
        BigInteger,
        Boolean,
        CHAR,
        Date,
        DateTime,
        Enum,
        Integer,
        LargeBinary,
        Numeric,
        SmallInteger,
        String,
        Text,
    )
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

    t = col.type  # type: ignore[attr-defined]
    if isinstance(t, UUID):
        return "UUID"
    if isinstance(t, JSONB):
        return "JSONB"
    if isinstance(t, ARRAY):
        return "ARRAY"
    if isinstance(t, LargeBinary):
        return "LARGEBINARY"
    if isinstance(t, BigInteger):
        return "BIGINTEGER"
    if isinstance(t, SmallInteger):
        return "SMALLINTEGER"
    if isinstance(t, Integer):
        return "INTEGER"
    if isinstance(t, Boolean):
        return "BOOLEAN"
    if isinstance(t, Numeric):
        p, s = t.precision, t.scale
        return f"NUMERIC({p},{s})" if p is not None else "NUMERIC"
    if isinstance(t, DateTime):
        return "DATETIME"
    if isinstance(t, Date):
        return "DATE"
    if isinstance(t, Text):
        return "TEXT"
    if isinstance(t, CHAR):
        return f"CHAR({t.length})"
    if isinstance(t, String):
        return f"VARCHAR({t.length})" if t.length else "VARCHAR"
    if isinstance(t, Enum):
        # SQLite renders Enum as VARCHAR(max_label_length).
        return "ENUM"
    return type(t).__name__.upper()


def _types_compatible(sqlite_type: str, pg_type_name: str) -> bool:
    """Return True if *sqlite_type* is a valid narrow form of *pg_type_name*.

    Rules are documented in the module docstring.
    """
    s = sqlite_type.upper()
    p = pg_type_name.upper()

    # Exact match is always OK.
    if s == p:
        return True

    # UUID → CHAR(32)
    if p == "UUID" and s == "CHAR(32)":
        return True

    # JSONB / ARRAY(*) → JSON
    if p in ("JSONB", "ARRAY") and s == "JSON":
        return True

    # BigInteger → BIGINT
    if p == "BIGINTEGER" and s == "BIGINT":
        return True

    # SmallInteger → SMALLINT
    if p == "SMALLINTEGER" and s in ("SMALLINT", "INTEGER"):
        return True

    # Integer / SmallInteger / BigInteger / Enum → INTEGER
    if p in ("INTEGER", "BIGINTEGER", "SMALLINTEGER", "ENUM") and s == "INTEGER":
        return True

    # Enum, String(n), Text → VARCHAR(n) / VARCHAR / TEXT
    if p in ("ENUM", "TEXT") and s in ("TEXT", "VARCHAR"):
        return True

    # String(n) renders as VARCHAR(n) on SQLite; length narrowing must match.
    if p.startswith("VARCHAR") and s.startswith("VARCHAR"):
        # Both VARCHAR with no length, or same length.
        return p == s
    if p.startswith("VARCHAR") and s == "TEXT":
        return True
    if p == "TEXT" and s.startswith("VARCHAR"):
        return True

    # CHAR(n) → CHAR(n) exact
    if p.startswith("CHAR") and s.startswith("CHAR"):
        return p == s

    # Numeric(p,s) → exact match required
    if p.startswith("NUMERIC") and s.startswith("NUMERIC"):
        # Normalise spacing: "NUMERIC(14, 2)" vs "NUMERIC(14,2)"
        p_norm = re.sub(r"\s", "", p)
        s_norm = re.sub(r"\s", "", s)
        return p_norm == s_norm

    # Boolean, Date, DateTime — already handled by exact match above.
    # LargeBinary → BLOB
    if p == "LARGEBINARY" and s == "BLOB":
        return True

    return False


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_cashbook_schema_file_exists() -> None:
    """Fail fast if the schema dump is missing (script was not run)."""
    assert _SCHEMA_PATH.exists(), (
        f"{_SCHEMA_PATH} not found. "
        "Run: python scripts/dump_cashbook_schema.py"
    )


def test_cashbook_strict_subset() -> None:
    """Every (table, column) in cashbook.schema.sql must exist in the Postgres
    ORM schema with a compatible type.

    Postgres-only tables and columns (those absent from Cashbook) are
    explicitly allowed — they are dropped on downgrade.

    Any failure in this test indicates one of:
    - A Cashbook table or column that is not present in the Postgres models
      (breaks lossless upgrade — must be fixed before shipping).
    - A type mismatch that is NOT covered by the documented narrowing rules
      (could silently corrupt data on upgrade — must be fixed or the rule
      must be explicitly extended here with a rationale comment).
    """
    assert _SCHEMA_PATH.exists(), (
        f"Schema dump not found: {_SCHEMA_PATH}. "
        "Run: python scripts/dump_cashbook_schema.py"
    )

    sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
    sqlite_cols = _parse_schema(sql_text)

    assert sqlite_cols, "Parser returned no columns — schema dump may be empty or malformed"

    pg_tables = Base.metadata.tables

    failures: list[str] = []

    for sc in sqlite_cols:
        # ------------------------------------------------------------------ #
        # Table must exist in Postgres ORM.                                  #
        # ------------------------------------------------------------------ #
        if sc.table not in pg_tables:
            failures.append(
                f"TABLE MISSING IN POSTGRES: {sc.table!r} "
                f"(has column {sc.name!r} of type {sc.sqlite_type!r})"
            )
            continue  # no point checking columns if table absent

        pg_table = pg_tables[sc.table]

        # ------------------------------------------------------------------ #
        # Column must exist in the Postgres table.                           #
        # ------------------------------------------------------------------ #
        if sc.name not in pg_table.c:
            failures.append(
                f"COLUMN MISSING IN POSTGRES: {sc.table}.{sc.name} "
                f"(SQLite type {sc.sqlite_type!r})"
            )
            continue

        # ------------------------------------------------------------------ #
        # Types must be compatible.                                          #
        # ------------------------------------------------------------------ #
        pg_col = pg_table.c[sc.name]
        pg_type = _postgres_type_name(pg_col)

        if not _types_compatible(sc.sqlite_type, pg_type):
            failures.append(
                f"TYPE MISMATCH: {sc.table}.{sc.name} — "
                f"SQLite {sc.sqlite_type!r} not compatible with "
                f"Postgres {pg_type!r} ({type(pg_col.type).__name__})"
            )

    if failures:
        msg = (
            f"\n\nStrict-subset invariant violated ({len(failures)} issue(s)).\n"
            "These failures mean Cashbook SQLite schema is NOT a strict subset\n"
            "of the Postgres schema, breaking lossless upgrade.\n\n"
            + "\n".join(f"  [{i+1}] {f}" for i, f in enumerate(failures))
        )
        pytest.fail(msg)


def test_cashbook_sqlite_table_count() -> None:
    """Smoke-test that the schema dump has at least 60 tables.

    The count will grow over time; this catches a broken dump that produces
    an empty or near-empty file.
    """
    assert _SCHEMA_PATH.exists(), f"Schema dump not found: {_SCHEMA_PATH}"
    sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
    cols = _parse_schema(sql_text)
    tables = {c.table for c in cols}
    assert len(tables) >= 60, (
        f"Expected at least 60 tables in cashbook.schema.sql, "
        f"got {len(tables)}. The dump may be truncated."
    )
