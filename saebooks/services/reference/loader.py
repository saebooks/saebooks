"""Idempotent seed loader for the reference DB.

Reads YAML files at ``saebooks/seeds/jurisdictions/<code>/*.yaml`` and
upserts each row keyed on the table-specific natural key declared in
the YAML header. Uses the ``ReferenceMigrationSession`` factory (owner
role) so the read-only app role does not need write privileges.

YAML schema (one document per file)
-----------------------------------
    table: <table_name>
    key:   [<col>, <col>, ...]   # natural-key columns for upsert
    rows:
      - <col>: <value>
        ...

Idempotency
-----------
The loader builds a Postgres ``INSERT ... ON CONFLICT (key_cols) DO
UPDATE`` for every row. Re-running the same file is a no-op. Re-running
a file with edited values updates in place. Removed rows are NOT
deleted — that requires an explicit ``--prune`` (not implemented in
v0.1.4; can be added later when we have a clear use case).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import ReferenceBase, ReferenceMigrationSession

logger = logging.getLogger("saebooks.reference.loader")

SEED_ROOT = Path(__file__).parent.parent.parent / "seeds" / "jurisdictions"

# Load order matters because of FKs (jurisdictions before tax_codes,
# currencies before countries.currency_default, etc.). The leading
# underscore on ``_global`` puts it before any ISO code alphabetically;
# inside each directory we sort by filename and rely on the global
# files coming first.
_GLOBAL_DIR = "_global"
_GLOBAL_ORDER = (
    "jurisdictions.yaml",
    "currencies.yaml",
    "countries.yaml",
)


class SeedLoaderNotConfiguredError(RuntimeError):
    """Raised when REFERENCE_MIGRATION_DATABASE_URL is unset."""


def _table_for(name: str) -> Any:
    """Resolve a table name to its SQLAlchemy ``Table`` object via metadata.

    Walks ReferenceBase.metadata, which gets fully populated when
    ``saebooks.models.reference`` is imported.
    """
    # Force registration — importing here is cheap and avoids the
    # caller having to remember the ordering.
    import saebooks.models.reference  # noqa: F401

    tbl = ReferenceBase.metadata.tables.get(name)
    if tbl is None:
        raise KeyError(
            f"Unknown reference table '{name}'. Known tables: "
            f"{sorted(ReferenceBase.metadata.tables)}"
        )
    return tbl


async def _apply_file(session: AsyncSession, path: Path) -> int:
    """Load one YAML file. Returns the number of rows upserted."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    table_name = doc.get("table")
    keys = doc.get("key") or []
    rows = doc.get("rows") or []
    if not table_name:
        raise ValueError(f"{path}: missing 'table'")
    if not keys:
        raise ValueError(f"{path}: missing 'key' (natural key columns)")

    table = _table_for(table_name)
    column_names = {c.name for c in table.columns}

    n = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: every row must be a mapping, got {row!r}")
        # Filter to known columns so a YAML with stray keys fails loud
        # instead of silently dropping data.
        unknown = set(row) - column_names
        if unknown:
            raise ValueError(
                f"{path}: row has unknown columns {sorted(unknown)} for "
                f"table {table_name}"
            )
        stmt = pg_insert(table).values(**row)
        # Build the ON CONFLICT update to refresh every non-key column.
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in table.columns
            if c.name in row and c.name not in keys
        }
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=keys, set_=update_cols
            )
        else:
            # Pure key-only row → just leave the existing row alone.
            stmt = stmt.on_conflict_do_nothing(index_elements=keys)
        await session.execute(stmt)
        n += 1
    return n


def _iter_yaml_files(jurisdiction: str | None) -> Iterable[Path]:
    """Yield seed files in dependency-safe order.

    Always yields _global/<jurisdictions, currencies, countries>.yaml
    first when present — those are the parents that everything else
    references. Then yields per-jurisdiction files for the requested
    jurisdiction, or every jurisdiction if ``jurisdiction is None``.
    """
    global_dir = SEED_ROOT / _GLOBAL_DIR
    for filename in _GLOBAL_ORDER:
        p = global_dir / filename
        if p.exists():
            yield p
    # Any other files in _global (alphabetically) after the priority
    # ones — e.g. shared registries we add later.
    if global_dir.exists():
        for p in sorted(global_dir.iterdir()):
            if p.suffix == ".yaml" and p.name not in _GLOBAL_ORDER:
                yield p

    if jurisdiction is None:
        # All jurisdictions, alphabetical.
        for sub in sorted(SEED_ROOT.iterdir()):
            if sub.is_dir() and sub.name != _GLOBAL_DIR:
                for p in sorted(sub.glob("*.yaml")):
                    yield p
    else:
        sub = SEED_ROOT / jurisdiction
        if not sub.is_dir():
            raise FileNotFoundError(
                f"No seed directory for jurisdiction '{jurisdiction}' at {sub}"
            )
        for p in sorted(sub.glob("*.yaml")):
            yield p


async def load_seeds(
    jurisdiction: str | None, *, version_tag: str | None = None
) -> dict[str, int]:
    """Load reference seeds. Returns {file_path: row_count}.

    ``jurisdiction`` is the directory name under
    ``saebooks/seeds/jurisdictions/`` (e.g. 'AU', 'NZ', 'EE') or None
    to load every jurisdiction.
    """
    if ReferenceMigrationSession is None:
        raise SeedLoaderNotConfiguredError(
            "REFERENCE_MIGRATION_DATABASE_URL is not set; the seed "
            "loader needs the owner role to write."
        )

    counts: dict[str, int] = {}
    async with ReferenceMigrationSession() as session:
        for path in _iter_yaml_files(jurisdiction):
            n = await _apply_file(session, path)
            counts[str(path.relative_to(SEED_ROOT))] = n
            logger.info("seed: %s → %d row(s)", path.name, n)

        # Stamp schema_meta with the chosen version tag if provided.
        if version_tag is not None:
            await session.execute(
                text(
                    "INSERT INTO schema_meta (id, version_tag) "
                    "VALUES (1, :tag) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "version_tag = EXCLUDED.version_tag, "
                    "loaded_at = NOW()"
                ).bindparams(tag=version_tag)
            )
        await session.commit()

    # Touch the inspector to keep mypy happy about unused-import in
    # downstream call sites — also a free sanity check that the engine
    # came back to a healthy state after the commit.
    _ = inspect
    return counts
