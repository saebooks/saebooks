"""Ad-hoc SQL browser service — run queries against the database.

This module hosts BOTH:

1. The **legacy** Jinja-admin surface (``run_query`` + helpers) used by
   ``saebooks/routers/admin.py``. The Cat-C rollup will retire that
   router; until then it must keep working.

2. The **Cat-C** v1 admin surface (``execute`` + helpers) used by
   ``saebooks/api/v1/admin.py``. This is the security-critical
   replacement.

Cat-C safety model (defense in depth):

1. Every plain SELECT runs on a **separate Postgres connection** opened
   as the ``saebooks_sql_ro`` role (migration 0087). That role has
   ``pg_read_all_data`` and explicit REVOKEs on
   ``pg_read_server_files`` / ``pg_write_server_files`` /
   ``pg_read_binary_file`` / ``lo_export`` / ``lo_import`` — Postgres
   refuses any DML or filesystem access at the role level.
2. Writes (INSERT/UPDATE/DELETE/ALTER/DROP/TRUNCATE) require an inline
   ``WriteConfirmation`` whose ``verb_typed`` matches the leading verb
   of the statement (case-insensitive). Without a matching confirmation
   the call raises ``WriteRejectedError`` — and an audit row is still
   written so the attempt is recorded.
3. Confirmed writes run on the runtime app engine (``saebooks_app``).
   That role is NOBYPASSRLS, so RLS still applies — the SQL tool can
   never reach across tenants.
4. Tenant binding is set via ``SET LOCAL app.current_tenant = …`` on
   both the RO and the RW connection so every RLS predicate that reads
   the GUC sees the caller's tenant.
5. EVERY statement — successful, rejected, or errored — appends one
   row to ``change_log`` with ``entity='sql_tool'`` and a JSONB payload
   carrying the statement, role, status, rowcount, timestamp,
   user_id and tenant_id.

The legacy surface ships its own (older, weaker) safety: a read-only
transaction + naive multi-statement detection. We don't tighten it
further because the v1 path is the long-term replacement.
"""
from __future__ import annotations

import csv
import io
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from saebooks.config import settings
from saebooks.db import engine as _runtime_engine
from saebooks.models.change_log import ChangeLog
from saebooks.models.sql_query import SqlQuery

# Max rows returned to the browser. Larger results force export to CSV.
RESULT_LIMIT = 500


class QueryError(Exception):
    """Raised for anything that prevents a query from running or returning
    a usable result. Used by both the legacy and Cat-C surfaces."""


# ---------------------------------------------------------------------------
# Cat-C — write classification
# ---------------------------------------------------------------------------
# Statements whose leading verb is in ``_WRITE_VERBS`` will be sent to the
# RW engine. Of those, only ``_CONFIRMABLE_VERBS`` may be confirmed by the
# admin UI; the rest (e.g. CREATE) are rejected outright because they
# rarely make sense from an audit perspective and side-step migration
# tracking.
_WRITE_VERBS: set[str] = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "CREATE",
    "GRANT",
    "REVOKE",
    "COPY",
    "MERGE",
}
_CONFIRMABLE_VERBS: set[str] = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "DROP",
    "TRUNCATE",
}


_LEADING_VERB_RE = re.compile(r"^\s*([A-Za-z][A-Za-z_]*)")


def leading_verb(statement: str) -> str:
    """Return the uppercased first SQL verb in ``statement``.

    Strips ``WITH …`` CTE prefixes so ``WITH x AS (…) UPDATE foo …``
    classifies as ``UPDATE`` rather than ``WITH``. Returns ``""`` for
    empty input.
    """
    s = statement.lstrip()
    while True:
        m = _LEADING_VERB_RE.match(s)
        if m is None:
            return ""
        verb = m.group(1).upper()
        if verb != "WITH":
            return verb
        # Skip the CTE definition: find the next top-level SELECT/INSERT/…
        # by scanning past balanced parentheses then whitespace.
        i = m.end()
        # skip the CTE name and AS keyword
        depth = 0
        in_paren = False
        while i < len(s):
            ch = s[i]
            if ch == "(":
                depth += 1
                in_paren = True
            elif ch == ")":
                depth -= 1
                if depth == 0 and in_paren:
                    i += 1
                    break
            i += 1
        # After the closing paren, look for "," (more CTEs) or the verb.
        rest = s[i:].lstrip()
        if rest.startswith(","):
            s = rest[1:].lstrip()
            continue
        s = rest
        m2 = _LEADING_VERB_RE.match(s)
        return m2.group(1).upper() if m2 else ""


def is_write_statement(statement: str) -> bool:
    return leading_verb(statement) in _WRITE_VERBS


# ---------------------------------------------------------------------------
# Cat-C — public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteConfirmation:
    """Inline ack that an admin meant to run a write.

    ``enabled`` must be ``True`` AND ``verb_typed.upper().strip()`` must
    equal the leading verb of the statement. Anything else triggers a
    ``WriteRejectedError`` — the audit row is still written.
    """

    enabled: bool = False
    verb_typed: str = ""


@dataclass
class ExecuteResult:
    rows: list[list[Any]]
    columns: list[str]
    rowcount: int
    role_used: str
    audit_id: int
    truncated: bool = False


class WriteRejectedError(Exception):
    """A write statement reached ``execute()`` without a matching
    confirmation. The audit row is already written; ``audit_id`` is
    surfaced so the API can echo it to the caller."""

    def __init__(self, message: str, *, audit_id: int) -> None:
        super().__init__(message)
        self.audit_id = audit_id


# ---------------------------------------------------------------------------
# Cat-C — read-only engine
# ---------------------------------------------------------------------------
#
# Lazy module-level cache: build once per password change. The engine
# is created with NullPool so each call opens a fresh connection — the
# RLS GUC is set with SET LOCAL and naturally torn down on commit/close.

_ro_engine_cache: dict[str, AsyncEngine] = {}


def _ro_database_url() -> str:
    """Rebuild the runtime DB URL with user/password swapped to
    ``saebooks_sql_ro`` / ``settings.sql_ro_password``.

    Falls back to ``DATABASE_URL`` if ``SAEBOOKS_APP_DATABASE_URL`` is
    unset (matches ``saebooks.db._runtime_database_url``).
    """
    base = settings.app_database_url or settings.database_url
    if not base:
        raise QueryError(
            "DATABASE_URL is not configured; cannot build read-only engine."
        )
    pw = settings.sql_ro_password
    if not pw:
        raise QueryError(
            "SAEBOOKS_SQL_RO_PASSWORD is not set — migration 0087 requires it."
        )
    parsed = urlparse(base)
    # Reconstruct the netloc with our role + password.
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"saebooks_sql_ro:{quote_plus(pw)}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _get_ro_engine() -> AsyncEngine:
    pw = settings.sql_ro_password
    if not pw:
        raise QueryError(
            "SAEBOOKS_SQL_RO_PASSWORD is not set — migration 0087 requires it."
        )
    cached = _ro_engine_cache.get(pw)
    if cached is not None:
        return cached
    eng = create_async_engine(
        _ro_database_url(), echo=False, future=True, poolclass=NullPool
    )
    _ro_engine_cache[pw] = eng
    return eng


# ---------------------------------------------------------------------------
# Cat-C — audit row writer
# ---------------------------------------------------------------------------


async def _append_audit(
    audit_session: AsyncSession,
    *,
    statement: str,
    role_used: str,
    status: str,
    rowcount: int,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    error: str | None,
) -> int:
    """Append one row to ``change_log`` and return its id.

    The audit_session is committed so the row survives even if the
    statement itself rolled back. ``entity='sql_tool'`` is the
    discriminator; the JSONB payload carries the rich detail.
    """
    payload: dict[str, Any] = {
        "statement": statement,
        "role_used": role_used,
        "status": status,
        "rowcount": rowcount,
        "ts": datetime.now(UTC).isoformat(),
        "user_id": str(user_id) if user_id is not None else None,
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
    }
    if error is not None:
        payload["error"] = error
    actor = f"user:{user_id}" if user_id is not None else "user:unknown"
    row = ChangeLog(
        entity="sql_tool",
        entity_id=uuid.uuid4(),
        op="execute",
        actor=actor,
        payload=payload,
        version=0,
    )
    audit_session.add(row)
    await audit_session.commit()
    await audit_session.refresh(row)
    return row.id


# ---------------------------------------------------------------------------
# Cat-C — connection runner
# ---------------------------------------------------------------------------


async def _run_on_role(
    engine: AsyncEngine,
    *,
    statement: str,
    tenant_id: uuid.UUID | None,
) -> tuple[list[str], list[list[Any]], int, bool]:
    """Open a fresh connection, bind tenant via ``SET LOCAL``, execute.

    Returns ``(columns, rows, rowcount, truncated)``. Rows are capped at
    ``RESULT_LIMIT``; one extra row is fetched to flip the ``truncated``
    flag.
    """
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    rowcount = 0
    async with engine.connect() as conn:
        # SET LOCAL is a Postgres GUC; on SQLite there is no per-txn
        # variable and the admin-SQL tool is a Postgres-only feature
        # anyway (RO/RW role split, pg_read_all_data). Skip the bind
        # if we're not on Postgres so the engine doesn't trip on
        # syntax — callers should already have gated this code path
        # with ``backend_supports_rls()``.
        if tenant_id is not None and conn.dialect.name == "postgresql":
            await conn.execute(
                text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
            )
        result = await conn.execute(text(statement))
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(RESULT_LIMIT + 1)
            if len(fetched) > RESULT_LIMIT:
                truncated = True
                fetched = fetched[:RESULT_LIMIT]
            rows = [list(r) for r in fetched]
            rowcount = len(rows)
        else:
            rowcount = result.rowcount or 0
        await conn.commit()
    return columns, rows, rowcount, truncated


# ---------------------------------------------------------------------------
# Cat-C — public entry point
# ---------------------------------------------------------------------------


async def execute(
    audit_session: AsyncSession,
    *,
    statement: str,
    write_confirmation: WriteConfirmation | None = None,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> ExecuteResult:
    """Execute one SQL statement on the admin SQL tool.

    Routing rules:

    * Read (leading verb not in ``_WRITE_VERBS``) → ``saebooks_sql_ro``
      engine (lazy + NullPool). RLS-bound to ``tenant_id``.
    * Write whose verb is in ``_CONFIRMABLE_VERBS`` AND has a matching
      ``WriteConfirmation`` (enabled + verb_typed.upper().strip() ==
      leading verb) → runtime engine (``saebooks_app``). RLS still
      applies; statement runs in a normal transaction and is committed.
    * Anything else (write without confirmation, write with the wrong
      verb typed, write whose verb is not confirmable like CREATE) →
      ``WriteRejectedError``. Audit row recorded.

    The audit row is always appended on a separate session-commit so it
    survives the case where the statement itself rolls back.
    """
    s = statement.strip()
    if not s:
        # Empty queries record nothing and raise — there's no statement
        # to audit, and this only happens on a coding bug in the caller.
        raise QueryError("Statement is empty.")

    verb = leading_verb(s)
    is_write = verb in _WRITE_VERBS
    role = "saebooks_sql_ro"
    if is_write:
        role = "saebooks_app"

    # Validate write confirmation BEFORE running anything.
    if is_write:
        wc = write_confirmation or WriteConfirmation()
        typed = (wc.verb_typed or "").upper().strip()
        if not wc.enabled or verb not in _CONFIRMABLE_VERBS or typed != verb:
            audit_id = await _append_audit(
                audit_session,
                statement=s,
                role_used=role,
                status="rejected",
                rowcount=0,
                user_id=user_id,
                tenant_id=tenant_id,
                error="write rejected: missing or mismatched confirmation",
            )
            raise WriteRejectedError(
                f"Write statement ({verb}) requires a matching "
                "WriteConfirmation (enabled + verb_typed equals the "
                "leading verb).",
                audit_id=audit_id,
            )

    engine = _runtime_engine if is_write else _get_ro_engine()

    try:
        columns, rows, rowcount, truncated = await _run_on_role(
            engine, statement=s, tenant_id=tenant_id
        )
    except Exception as exc:
        audit_id = await _append_audit(
            audit_session,
            statement=s,
            role_used=role,
            status="error",
            rowcount=0,
            user_id=user_id,
            tenant_id=tenant_id,
            error=str(exc),
        )
        # Wrap as QueryError so callers don't have to catch raw asyncpg
        # / SQLAlchemy exceptions. Embed the audit_id by attribute.
        wrapped = QueryError(str(exc))
        wrapped.audit_id = audit_id  # type: ignore[attr-defined]
        raise wrapped from exc

    audit_id = await _append_audit(
        audit_session,
        statement=s,
        role_used=role,
        status="ok",
        rowcount=rowcount,
        user_id=user_id,
        tenant_id=tenant_id,
        error=None,
    )

    return ExecuteResult(
        rows=rows,
        columns=columns,
        rowcount=rowcount,
        role_used=role,
        audit_id=audit_id,
        truncated=truncated,
    )


# ===========================================================================
# Legacy Jinja-admin surface — kept verbatim for ``saebooks/routers/admin.py``
# ===========================================================================


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    duration_ms: int
    truncated: bool
    sql: str


@dataclass
class TableInfo:
    name: str
    columns: list[tuple[str, str]]  # (column_name, data_type)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _strip_sql(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _has_multiple_statements(sql: str) -> bool:
    """True if the SQL contains more than one statement (naive, strips strings
    and dollar-quoted blocks before checking for `;`).
    """
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < len(sql) and sql[i] != '"':
                i += 1
            i += 1
            continue
        if ch == "$" and i + 1 < len(sql) and sql[i + 1] == "$":
            i += 2
            while i + 1 < len(sql) and not (sql[i] == "$" and sql[i + 1] == "$"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return ";" in cleaned


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

async def list_tables(session: AsyncSession) -> list[TableInfo]:
    """Return tables+columns in the public schema, ordered by name."""
    rows = (await session.execute(text("""
        SELECT c.table_name, c.column_name, c.data_type
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_name = c.table_name AND t.table_schema = c.table_schema
        WHERE c.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
    """))).all()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for table, col, dtype in rows:
        grouped.setdefault(table, []).append((col, dtype))
    return [
        TableInfo(name=t, columns=cols)
        for t, cols in sorted(grouped.items())
    ]


# ---------------------------------------------------------------------------
# Query execution (legacy)
# ---------------------------------------------------------------------------

async def run_query(
    session: AsyncSession,
    sql: str,
    *,
    performed_by: str | None = None,
    log_history: bool = True,
) -> QueryResult:
    """Run a single read-only SQL query and return the result.

    Uses a separate connection + transaction so the calling session is
    untouched. The read-only flag is set at the Postgres transaction level,
    so any attempt to INSERT/UPDATE/DELETE is rejected by the database.
    """
    stripped = _strip_sql(sql)
    if not stripped:
        raise QueryError("Query is empty.")
    if _has_multiple_statements(stripped):
        raise QueryError(
            "Only a single statement is allowed. Split multi-statement "
            "queries into separate runs."
        )

    engine = session.bind
    if engine is None:  # pragma: no cover — every real session has a bind
        raise QueryError("No database engine available on this session.")

    start = time.perf_counter()
    error: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    row_count = 0

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            result = await conn.execute(text(stripped))
            if result.returns_rows:
                columns = list(result.keys())
                fetched = result.fetchmany(RESULT_LIMIT + 1)
                if len(fetched) > RESULT_LIMIT:
                    truncated = True
                    fetched = fetched[:RESULT_LIMIT]
                rows = [list(r) for r in fetched]
                row_count = len(rows)
            else:
                row_count = 0
    except Exception as exc:
        error = str(exc)
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if log_history:
            session.add(
                SqlQuery(
                    sql=stripped,
                    row_count=row_count,
                    duration_ms=duration_ms,
                    error=error,
                    performed_by=performed_by,
                )
            )
            await session.commit()

    if error is not None:
        raise QueryError(error)

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=row_count,
        duration_ms=duration_ms,
        truncated=truncated,
        sql=stripped,
    )


# ---------------------------------------------------------------------------
# History (legacy)
# ---------------------------------------------------------------------------

async def recent_queries(
    session: AsyncSession, *, limit: int = 20
) -> list[SqlQuery]:
    result = await session.execute(
        select(SqlQuery).order_by(SqlQuery.executed_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_query(
    session: AsyncSession, query_id: uuid.UUID
) -> SqlQuery | None:
    return await session.get(SqlQuery, query_id)


# ---------------------------------------------------------------------------
# CSV export (legacy)
# ---------------------------------------------------------------------------

def to_csv(columns: list[str], rows: list[list[Any]]) -> str:
    """Format a result as CSV text (UTF-8 with BOM so Excel opens it cleanly)."""
    buf = io.StringIO()
    buf.write("﻿")  # BOM
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue()
