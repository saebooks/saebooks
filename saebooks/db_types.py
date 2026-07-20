"""Cross-dialect type wrappers and compile hooks.

This module exists so the saebooks codebase can run against both
Postgres (the canonical, RLS-enforced production backend) and SQLite
(the Cashbook single-tenant local backend on mobile / desktop).

What it does
------------
* Registers ``@compiles(..., "sqlite")`` hooks so that
  ``postgresql.JSONB`` / ``postgresql.ARRAY`` render as ``JSON`` and
  ``postgresql.UUID`` renders as ``CHAR(32)`` on SQLite. See the
  individual hook docstrings for the affinity / DDL reasoning. These
  cover both the ORM ``Column`` references and the inline
  ``postgresql.JSONB`` / ``UUID`` references in alembic migrations.
* Provides ``JSONB``/``UUID``/``ARRAY`` re-exports from
  ``sqlalchemy.dialects.postgresql`` so model code can keep using the
  names it already uses; the import path is the only thing that
  changes if anyone wants the wrapped names instead.

Why a compile hook rather than a TypeDecorator wrapper
------------------------------------------------------
The model layer alone uses 60+ ``postgresql.JSONB`` references and
30+ alembic migrations use them inline at DDL time. A compile hook
is a one-time install at import time and covers both call sites with
zero edits. TypeDecorator only solves the ORM side; alembics
``op.add_column(... postgresql.JSONB)`` would still blow up on SQLite.

The hook is safe under Postgres because its scoped to the
``"sqlite"`` dialect and never fires there.

Why JSON rather than a structured value type on SQLite
------------------------------------------------------
The Cashbook use of these columns is opaque blob storage: audit
snapshots, address dicts, cashbook-category overrides. None of the
Cashbook code paths use Postgres-specific JSONB operators
(``@>``, ``->>``, ``?``). Tests that do hit those operators are
marked ``postgres_only`` and skipped on SQLite. See
``pyproject.toml`` ``markers`` table.

Why CHAR(32) for UUID on SQLite
-------------------------------
SQLite columns get type *affinity* from the declared type name.
``UUID`` matches no affinity rule, so SQLite assigns NUMERIC affinity,
and ``00000000000000000000000000000001`` parses as the integer 1 at
INSERT time. ``CHAR(32)`` gives TEXT affinity so the 32-hex form
stored by SQLAlchemy's UUID bind processor survives the round-trip
intact. Verified 2026-05-14 against the default seed tenant.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import JSON, Numeric, TypeDecorator
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles

# --------------------------------------------------------------------------- #
# SQLite compile hooks for Postgres-native column types                       #
# --------------------------------------------------------------------------- #
# These fire only when the SQL is being compiled for the SQLite dialect.
# Under Postgres the hooks are skipped and the native JSONB / ARRAY DDL
# is emitted unchanged.

@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    return compiler.visit_JSON(JSON(), **kw)


@compiles(ARRAY, "sqlite")
def _array_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    return compiler.visit_JSON(JSON(), **kw)


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """Emit ``CHAR(32)`` for postgresql.UUID columns on SQLite.

    Why this matters: SQLite uses type *affinity* (not declared types)
    to coerce input values. A column declared ``UUID`` is assigned
    NUMERIC affinity, which means SQLite will try to parse INSERTed
    values as decimals and integers. The 32-hex UUID form
    ``00000000000000000000000000000001`` parses as the integer 1, so
    on subsequent SELECT the row's id comes back as a Python int.
    SQLAlchemy's UUID result_processor then crashes:

        AttributeError: 'int' object has no attribute 'replace'

    Declaring the column as ``CHAR(32)`` gives it TEXT affinity, which
    preserves the hex string verbatim. The bind/result processors
    SQLAlchemy 2.x ships for ``Uuid`` continue to convert between
    ``uuid.UUID`` and the 32-hex string transparently — only the DDL
    needs adjusting.

    Reproduced 2026-05-14 on the default seed tenant
    ``00000000-0000-0000-0000-000000000001``; without this hook the
    INSERT-then-SELECT round-trip silently turns the UUID into 1.
    """
    return "CHAR(32)"


@compiles(INET, "sqlite")
def _inet_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """Emit ``VARCHAR`` for postgresql.INET columns on SQLite.

    INET is a Postgres-native type with no SQLite analogue; the only
    consumer on SQLite would be the ``ephemeral_demo_tenants`` control
    table, which never runs on the Cashbook backend (ephemeral demos are
    a public-preview Postgres feature). The hook exists purely so
    ``Base.metadata.create_all`` on a SQLite engine — used by
    ``bootstrap_schema`` and the SQLite test path, which import every
    model — does not blow up at DDL compile time on the unknown type.
    Stored values are plain dotted-quad / IPv6 strings, which TEXT
    affinity preserves verbatim.
    """
    return "VARCHAR"


# --------------------------------------------------------------------------- #
# Money — Numeric(18, 4) storage, minor-unit presentation on read             #
# --------------------------------------------------------------------------- #

_MONEY_TRIM_QUANTA = (Decimal("0.01"), Decimal("0.001"))


class Money(TypeDecorator):
    """Monetary column: ``Numeric(18, 4)`` storage, trimmed on read.

    Storage is widened to 4 decimal places so sub-cent minor units
    (mils, three-decimal dinars) fit, but the DB pads every value to
    the column scale — an AUD ``1.23`` written by a service comes back
    as ``Decimal("1.2300")``, and API serialization (pydantic renders
    ``Decimal`` as a JSON string) would present ``"1.2300"`` where the
    pre-widening schema produced ``"1.23"``.

    ``process_result_value`` therefore strips value-preserving trailing
    zeros down to a floor of 2 decimal places. Services quantize on
    write to the currency's ISO-4217 minor unit (``saebooks.money``),
    so a stored value never carries non-zero digits beyond its
    currency's places: AU/2-place reads reproduce the pre-widening
    bytes exactly, 3/4-place currencies keep their full precision, and
    the trim never *rounds* (a quantum is only applied when it leaves
    the value unchanged).
    """

    impl = Numeric(18, 4)
    cache_ok = True

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int) or exponent >= -2:
            return value
        for quantum in _MONEY_TRIM_QUANTA:
            trimmed = value.quantize(quantum)
            if trimmed == value:
                return trimmed
        return value


__all__ = ["ARRAY", "INET", "JSONB", "UUID", "Money"]
