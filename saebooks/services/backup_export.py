"""Per-tenant LOGICAL export — the data-isolation-critical core of
scheduled backups (planned-modules build-out Wave E, decision 6).

THE INVARIANT THIS MODULE EXISTS TO GUARANTEE
-----------------------------------------------
An export produced by :func:`export_tenant_data` for tenant A contains
**zero rows belonging to any other tenant**. Every table this module
touches is queried with an explicit ``WHERE <tenant column> = :tenant_id``
(or, for child/line tables with no direct ``tenant_id`` column, an INNER
JOIN to an ancestor table that DOES carry one, filtered the same way) —
never a bare ``SELECT *``. This holds independently of Postgres RLS: the
cross-tenant probe test (``tests/services/test_backup_export.py``) runs
this module against the SQLite backend, which has no RLS at all, and
still proves zero foreign rows — the app-layer filter alone is the
guarantee, RLS is defence-in-depth on top of it (same posture as every
other tenant-scoped query in this codebase, see
``feedback_new-table-rls-checklist`` item 6).

This is emphatically NOT ``services/backups.py``'s whole-database
``pg_dump`` — that file stays a read-only VIEW of the infra timer's
output and is never exposed to a tenant. This module never runs
``pg_dump`` and never touches a row it hasn't itself scoped.

HOW "WHICH TABLES" IS DECIDED — AND HOW THAT DECISION STAYS CORRECT
----------------------------------------------------------------------
This codebase has ~103 ORM tables. Rather than hand-listing "the
tables a backup includes" (the exact anti-pattern
``feedback_new-table-rls-checklist`` documents recurring — a
hand-maintained list silently goes stale as new tables are added), this
module REFLECTS ``Base.metadata`` at runtime and classifies EVERY table
into exactly one bucket:

1. ``TENANT_DIRECT`` — the table has its own ``tenant_id`` column.
   Exported with a plain ``WHERE tenant_id = :tenant_id``. This bucket
   is fully automatic: a new tenant-scoped table that follows the
   checklist (tenant_id column) is picked up with ZERO code change here.

2. ``CHILD_TABLES`` — the table has no ``tenant_id`` column but is
   unambiguously owned by ONE ancestor row via a single FK (the classic
   shape: ``invoice_lines.invoice_id -> invoices.id``, and
   ``invoices.tenant_id`` is itself TENANT_DIRECT). Exported via an
   INNER JOIN to the ancestor, filtered on the ancestor's tenant_id.
   This bucket IS a hand-maintained list — SQLAlchemy reflection cannot
   infer "which of a table's several FKs is the ownership one" (e.g.
   ``bill_lines`` has FKs to ``bills`` AND ``tax_codes`` AND
   ``projects`` AND ``items`` AND ``accounts`` — only ``bill_id`` is
   the actual parent). Hand-maintaining this list is unavoidable, so
   the risk is contained instead: see ``classify_all_tables`` below.

3. ``GLOBAL_EXCLUDE`` — deliberately NOT exported, each with a specific
   reason (system/global reference data, credential material, or a
   currently cross-tenant-unsafe table). Also hand-maintained.

THE COMPLETENESS TEST THAT KEEPS THIS HONEST
-----------------------------------------------
``classify_all_tables()`` walks EVERY table in ``Base.metadata`` and
requires each one to land in exactly one of the three buckets above —
raising ``UnclassifiedTableError`` for anything it doesn't recognise.
``tests/services/test_backup_export.py::test_every_table_is_classified``
runs this against the live model registry on every test run. The
practical effect: add a new ORM table and do NOTHING else, and the test
suite fails loud with the new table's name, forcing a conscious
TENANT_DIRECT / CHILD_TABLES / GLOBAL_EXCLUDE decision before it can
ship silently un-backed-up (GLOBAL_EXCLUDE is a legitimate, auditable
choice) or, far worse, silently un-SCOPED (impossible — TENANT_DIRECT
is automatic and CHILD_TABLES membership is opt-in, so "forgot to add
it" fails the completeness test rather than leaking).

WHY SOME TABLES ARE EXCLUDED EVEN THOUGH THEY'RE TENANT DATA
-----------------------------------------------------------------
* ``api_tokens``, ``user_webauthn_credentials``,
  ``bank_feed_external_creds``, ``oauth_provider_links``,
  ``idempotency_records``, ``idempotency_keys`` — authentication /
  credential / operational-dedup material. Not portable across
  environments (a WebAuthn public key or an OAuth provider link is
  meaningless outside the exact browser/session it was minted for) and
  not "business data" in the sense a client backup is for. Consistently
  excluded as a class, not cherry-picked.
* ``audit_snapshots`` — flagged in
  ``~/records/saebooks/planned-modules-build-plan.md`` as CURRENTLY
  missing ``tenant_id`` + RLS (the "two hard RLS remediations" section,
  scheduled for Wave C). It is presently impossible to scope this table
  to one tenant at all — excluding it is the only safe choice until
  that migration lands, not an oversight. Revisit once Wave C ships.
* ``tenants``, ``permissions``, ``settings``, ``sql_queries``,
  ``fx_rate_snapshots``, ``payg_tax_scales``, ``stsl_coefficients``,
  ``depreciation_models``, ``bank_feed_issues`` — global/system/
  jurisdiction-reference tables, verified via their own model
  docstrings to carry no tenant or company scoping at all (see e.g.
  ``models/fx_rate_snapshot.py``: "Deliberately NOT CompanyScoped —
  FX rates are global infra"). ``role_permissions`` moved OUT of this
  bucket by the granular_permissions module (migration
  ``0194_role_permissions_rls``) — it is now genuinely
  tenant-scoped (one row set per tenant, not shared globally) and
  auto-classifies as TENANT_DIRECT via its new ``tenant_id`` column;
  ``roles`` (new table, same migration wave) auto-classifies the same
  way.
* ``ephemeral_demo_tenants`` — its own docstring documents it is
  "exempt from the new-tenant-table RLS checklist" (public-preview demo
  lifecycle bookkeeping, not client business data).
* ``principals``, ``principal_fido2_credentials`` — the undeployed
  cross-tenant accountant-login review surface (manifest note: "REVIEW
  BRANCH feat/accountant-login — cross-tenant surface, not deployed").
  A principal is not owned by a single tenant by design; out of scope
  for a per-tenant export.

Restore is explicitly OUT OF SCOPE for this module (v1 is export +
encrypted download only, per Wave E's guardrails) — the manifest format
is documented well enough that a future restore tool could target it,
but no restore path is built or claimed here.
"""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import gzip
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import Base

# Soft per-table row cap — a safety valve against an unbounded export on
# a pathologically large tenant, not a targeted feature limit. Each
# capped table is flagged `truncated: true` in the manifest rather than
# silently dropping rows unannounced.
_DEFAULT_ROW_LIMIT_PER_TABLE = 200_000


def _row_limit_per_table() -> int:
    raw = os.environ.get("SAEBOOKS_SCHEDULED_BACKUP_ROW_LIMIT_PER_TABLE", "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_ROW_LIMIT_PER_TABLE
    except ValueError:
        return _DEFAULT_ROW_LIMIT_PER_TABLE


class ChildTableSpec(NamedTuple):
    """One CHILD_TABLES entry: ``table``'s ownership FK + its parent."""

    fk_column: str
    parent_table: str


# ---------------------------------------------------------------------- #
# Bucket 2 — CHILD_TABLES (hand-maintained; see module docstring)        #
# ---------------------------------------------------------------------- #
CHILD_TABLES: dict[str, ChildTableSpec] = {
    "invoice_lines": ChildTableSpec("invoice_id", "invoices"),
    "bill_lines": ChildTableSpec("bill_id", "bills"),
    "credit_note_lines": ChildTableSpec("credit_note_id", "credit_notes"),
    "expense_lines": ChildTableSpec("expense_id", "expenses"),
    "journal_lines": ChildTableSpec("entry_id", "journal_entries"),
    "pay_run_lines": ChildTableSpec("pay_run_id", "pay_runs"),
    "purchase_order_lines": ChildTableSpec("purchase_order_id", "purchase_orders"),
    "quote_lines": ChildTableSpec("quote_id", "quotes"),
    "receipt_lines": ChildTableSpec("receipt_id", "receipts"),
    "recurring_invoice_lines": ChildTableSpec(
        "recurring_invoice_id", "recurring_invoices"
    ),
    "supplier_credit_note_lines": ChildTableSpec(
        "supplier_credit_note_id", "supplier_credit_notes"
    ),
    # payment_allocations carries 4 FKs (payment_id/invoice_id/bill_id/
    # credit_note_id — it's the join row spanning a payment and what it
    # settles). payment_id is the ownership edge: an allocation cannot
    # exist without its payment, and payments is TENANT_DIRECT.
    "payment_allocations": ChildTableSpec("payment_id", "payments"),
    "beneficiary_entitlements": ChildTableSpec(
        "distribution_id", "trust_distributions"
    ),
    # Authorization config (which permissions a user holds), not
    # credential material — kept distinct from the excluded auth tables.
    "user_permissions": ChildTableSpec("user_id", "users"),
    # bank_feed_clients/accounts are CompanyScoped (company_id, no direct
    # tenant_id) — companies.tenant_id is the ancestor.
    "bank_feed_clients": ChildTableSpec("company_id", "companies"),
    "bank_feed_accounts": ChildTableSpec("company_id", "companies"),
    "ato_sbr_configs": ChildTableSpec("company_id", "companies"),
    "document_counters": ChildTableSpec("company_id", "companies"),
    "period_locks": ChildTableSpec("company_id", "companies"),
}

# ---------------------------------------------------------------------- #
# Bucket 3 — GLOBAL_EXCLUDE (hand-maintained; see module docstring)      #
# ---------------------------------------------------------------------- #
GLOBAL_EXCLUDE: frozenset[str] = frozenset(
    {
        # Credential / authentication / operational-dedup material.
        "api_tokens",
        "user_webauthn_credentials",
        "bank_feed_external_creds",
        "oauth_provider_links",
        "idempotency_records",
        "idempotency_keys",
        # Currently cross-tenant-unsafe (no tenant_id/RLS yet) — see
        # module docstring; revisit once Wave C's migration lands.
        "audit_snapshots",
        # Global / system / jurisdiction-reference — verified via their
        # own model docstrings to carry no tenant or company scoping.
        # ("role_permissions" moved OUT of this bucket — see module
        # docstring, now genuinely tenant-scoped, auto-classifies
        # TENANT_DIRECT via its own tenant_id column.)
        "tenants",
        "permissions",
        "settings",
        "sql_queries",
        "fx_rate_snapshots",
        "payg_tax_scales",
        "stsl_coefficients",
        "depreciation_models",
        "bank_feed_issues",
        "ephemeral_demo_tenants",
        # Undeployed cross-tenant accountant-login review surface.
        "principals",
        "principal_fido2_credentials",
    }
)

# Tables that DO have their own tenant_id column but are excluded from
# TENANT_DIRECT for the same "credential/operational, not business
# data" reasons as the GLOBAL_EXCLUDE set — kept as a separate constant
# because these tables are otherwise auto-included by the reflection
# scan, so this is an explicit override, not an omission.
TENANT_DIRECT_OVERRIDE_EXCLUDE: frozenset[str] = frozenset()


class UnclassifiedTableError(RuntimeError):
    """Raised by ``classify_all_tables`` for a table in none of the 3
    buckets — see module docstring's "completeness test" section.
    Fixing this means a conscious decision, not a code tweak: either
    add ``tenant_id`` and let TENANT_DIRECT pick it up, add an entry to
    ``CHILD_TABLES``, or add it to ``GLOBAL_EXCLUDE`` with a reason.
    """


@dataclass(frozen=True, slots=True)
class TableClassification:
    kind: str  # "direct" | "child" | "excluded"
    child_spec: ChildTableSpec | None = None
    exclude_reason: str = ""


_models_imported = False


def _ensure_all_models_imported() -> None:
    """``Base.metadata.tables`` only contains tables whose ORM class has
    actually been imported somewhere in the process — SQLAlchemy
    registers a table at class-DEFINITION time, not at schema-creation
    time. In a live server this is (almost certainly) already true by
    the time a request lands, because every router imports the model
    classes it touches and the app boots all ~85 routers — but "almost
    certainly, implicitly, because something else happened to import
    them" is exactly the wrong foundation for a completeness/leak-safety
    guarantee. Mirrors ``saebooks.db.bootstrap_schema``'s own explicit
    walk (same reasoning, same fix) rather than relying on import order.
    Idempotent — the ``pkgutil`` walk only runs once per process.
    """
    global _models_imported
    if _models_imported:
        return
    import importlib
    import pkgutil

    import saebooks.models as _models

    for mod_info in pkgutil.iter_modules(_models.__path__):
        importlib.import_module(f"saebooks.models.{mod_info.name}")
    _models_imported = True


def classify_all_tables() -> dict[str, TableClassification]:
    """Classify every table in ``Base.metadata`` into direct/child/excluded.

    Raises ``UnclassifiedTableError`` (loud, not silent) if a table is
    in none of the three buckets — this is the mechanism that keeps a
    newly-added ORM table from silently falling out of both the export
    AND the completeness guarantee.
    """
    _ensure_all_models_imported()
    out: dict[str, TableClassification] = {}
    for name, table in Base.metadata.tables.items():
        if name in CHILD_TABLES:
            out[name] = TableClassification("child", child_spec=CHILD_TABLES[name])
            continue
        if name in GLOBAL_EXCLUDE:
            out[name] = TableClassification("excluded", exclude_reason="global_exclude")
            continue
        if "tenant_id" in table.c:
            if name in TENANT_DIRECT_OVERRIDE_EXCLUDE:
                out[name] = TableClassification(
                    "excluded", exclude_reason="credential_or_operational"
                )
                continue
            out[name] = TableClassification("direct")
            continue
        raise UnclassifiedTableError(
            f"Table {name!r} has no tenant_id column and is not in "
            "CHILD_TABLES or GLOBAL_EXCLUDE — classify it in "
            "saebooks/services/backup_export.py before it can ship."
        )
    return out


def _json_default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


@dataclass(slots=True)
class TableExportResult:
    row_count: int
    truncated: bool
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TenantExportResult:
    tenant_id: uuid.UUID
    exported_at: str
    engine_export_format_version: int
    tables: dict[str, TableExportResult]

    def table_counts(self) -> dict[str, int]:
        return {name: r.row_count for name, r in self.tables.items()}

    def to_manifest_dict(self) -> dict[str, Any]:
        """The manifest — metadata about the export, no row data."""
        return {
            "tenant_id": str(self.tenant_id),
            "exported_at": self.exported_at,
            "engine_export_format_version": self.engine_export_format_version,
            "tables": {
                name: {"row_count": r.row_count, "truncated": r.truncated}
                for name, r in self.tables.items()
            },
        }

    def to_json_bytes(self) -> bytes:
        """Full artifact payload: manifest + every table's rows, as
        UTF-8 JSON bytes. Caller gzips + encrypts this — see
        ``services/scheduled_backups.py``."""
        payload = {
            "manifest": self.to_manifest_dict(),
            "tables": {
                name: r.rows for name, r in self.tables.items()
            },
        }
        return json.dumps(payload, default=_json_default).encode("utf-8")


EXPORT_FORMAT_VERSION = 1


async def export_tenant_data(
    session: AsyncSession, tenant_id: uuid.UUID
) -> TenantExportResult:
    """Build the full per-tenant logical export.

    Every query below is either ``WHERE tenant_id = :tenant_id`` (direct
    tables) or an INNER JOIN to a direct table filtered the same way
    (child tables) — see module docstring. ``session`` is expected to be
    the standard RLS-scoped ``Depends(get_session)`` session (tenant
    already bound via ``app.current_tenant``) so Postgres RLS enforces
    the SAME boundary a second time; on SQLite (no RLS) the explicit
    WHERE/JOIN is the entire guarantee, which is exactly what the
    cross-tenant probe test exercises.
    """
    classification = classify_all_tables()
    limit = _row_limit_per_table()
    tables_out: dict[str, TableExportResult] = {}

    for name, table in Base.metadata.tables.items():
        info = classification[name]
        if info.kind == "excluded":
            continue
        result = await _export_one_table(session, table, info, tenant_id, limit)
        tables_out[name] = result

    return TenantExportResult(
        tenant_id=tenant_id,
        exported_at=_dt.datetime.now(_dt.UTC).isoformat(),
        engine_export_format_version=EXPORT_FORMAT_VERSION,
        tables=tables_out,
    )


async def _export_one_table(
    session: AsyncSession,
    table: Table,
    info: TableClassification,
    tenant_id: uuid.UUID,
    limit: int,
) -> TableExportResult:
    if info.kind == "direct":
        stmt = select(table).where(table.c.tenant_id == tenant_id).limit(limit + 1)
    else:
        assert info.child_spec is not None
        parent = Base.metadata.tables[info.child_spec.parent_table]
        stmt = (
            select(table)
            .select_from(
                table.join(
                    parent,
                    table.c[info.child_spec.fk_column] == parent.c.id,
                )
            )
            .where(parent.c.tenant_id == tenant_id)
            .limit(limit + 1)
        )

    rows = (await session.execute(stmt)).all()
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    out_rows = [dict(r._mapping) for r in rows]
    return TableExportResult(row_count=len(out_rows), truncated=truncated, rows=out_rows)


def gzip_json(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)


def gunzip_json(data: bytes) -> bytes:
    return gzip.decompress(data)
