"""Ad-hoc SQL browser service — run read-only queries against the database.

Safety model (defense in depth):
1. Every query runs inside `BEGIN; SET TRANSACTION READ ONLY; ...; ROLLBACK`.
   Postgres rejects any data-modifying statement at the transaction level.
2. We reject multi-statement queries (strip `;` then check for remaining
   semicolons outside quotes).
3. We apply a result-size cap (`RESULT_LIMIT`) so a mis-typed `SELECT *
   FROM large_table` can't OOM the app.
4. Every query — successful or not — is appended to the `sql_queries`
   history table so we always know what's been run.

This is meant as a developer/ops tool, not an end-user feature.
"""
from __future__ import annotations

import csv
import io
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.sql_query import SqlQuery


# Max rows returned to the browser. Larger results force export to CSV.
RESULT_LIMIT = 500


class QueryError(Exception):
    """Raised for anything that prevents a query from running or returning
    a usable result."""


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
    # Remove single-quoted strings, double-quoted identifiers, and $$...$$ blocks
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # consume until matching quote (with escape '')
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
            # dollar-quoted block
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
# Query execution
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
        # Fresh connection so we don't pollute the caller's session state.
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
                # A read-only query that doesn't return rows — e.g. SHOW, EXPLAIN
                # with no rows — just report 0.
                row_count = 0
            # Read-only transactions auto-close on connection exit; no commit.
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
# History
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
# CSV export
# ---------------------------------------------------------------------------

def to_csv(columns: list[str], rows: list[list[Any]]) -> str:
    """Format a result as CSV text (UTF-8 with BOM so Excel opens it cleanly)."""
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue()
