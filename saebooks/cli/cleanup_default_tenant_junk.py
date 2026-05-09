"""Soft-archive critic-test junk that landed in the Default Company.

Background
----------
Before the cross-tenant leak fix (audit-trail #02), three compounding
bugs caused all critic agents — regardless of their JWT ``tenant_id`` —
to write into the dev/seed Default Company
(``bd1f362e-4138-4304-87dc-40d8b5768a72``, tenant
``00000000-0000-0000-0000-000000000001``).  Bug 1 was
``_first_company_id()`` ignoring the caller and always returning the
oldest active company in the DB.  See
``02-cross-tenant-leak-diagnosis.md`` for the full root cause.

The leak is now closed (commits 3052b12, ff86620, 03d2868, b206b3c,
5c92c19, d8e697f).  This script cleans up the rows that landed in the
wrong place while the bug was live.

Why soft-archive, not DELETE
----------------------------
The diagnosis suggested ``DELETE FROM contacts WHERE company_id = ...``
but several FK constraints reference ``contacts.id`` with
``ON DELETE RESTRICT`` (invoices, bills, payments, credit_notes,
recurring_invoices), so a naive DELETE would fail.  Cascading deletes
across all five tables also wipes the ``change_log`` audit trail —
unhelpful for compliance and for future investor narrative ("yes we
had a P0 bug, here's the audit log").

Soft-archive (``archived_at = now()`` + ``version += 1`` + a
``change_log`` row with ``op='archive'``) achieves the operational
goal — the rows disappear from list endpoints (which all filter
``archived_at IS NULL``) — without the FK or audit-loss problems.  And
it is reversible: ``UPDATE … SET archived_at = NULL`` undoes it.

Scope
-----
The script targets ONLY the Default Company
(``bd1f362e-4138-4304-87dc-40d8b5768a72``).  It refuses to run if the
Company itself is already archived, or if the company id resolves to
anything other than "Default Company".

Tables in scope (data tables): contacts, invoices, bills, payments,
journal_entries.

Tables explicitly NOT in scope: accounts, tax_codes — those rows are
the canonical AU CoA / GST seed and must remain available.

POSTED rows
-----------
By default, only DRAFT rows are archived.  ``--include-posted`` also
archives POSTED invoices/bills/journal_entries.  These are accounting
artefacts that did make it into the GL during the buggy period; the
flag is opt-in so the operator confirms the intent.

Usage
-----
Dry-run (default — prints what would happen and exits)::

    python -m saebooks.cli.cleanup_default_tenant_junk

Apply (writes inside one transaction; rolls back on any error)::

    python -m saebooks.cli.cleanup_default_tenant_junk --apply

Apply including POSTED rows::

    python -m saebooks.cli.cleanup_default_tenant_junk --apply --include-posted

The script runs as the OWNER role (``AsyncSessionLocal``) which
bypasses RLS — that is intentional, since we need to see and modify
rows in the dev tenant regardless of any caller's tenant context.
The ``company_id = bd1f362e-...`` filter is the only scoping.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry
from saebooks.models.payment import Payment
from saebooks.services import change_log as change_log_svc

logger = logging.getLogger("saebooks.cli.cleanup_default_tenant_junk")

DEFAULT_COMPANY_ID = uuid.UUID("bd1f362e-4138-4304-87dc-40d8b5768a72")
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_COMPANY_NAME = "Default Company"
ACTOR = "cleanup_default_tenant_junk"

# (entity_name, model, label_attr) — entity_name matches the
# ``ChangeLog.entity`` convention used by per-table services.  The
# label_attr is what we print in the dry-run summary so the operator
# can scan the list before approving.
_TABLES: list[tuple[str, type, str]] = [
    ("contact", Contact, "name"),
    ("invoice", Invoice, "number"),
    ("bill", Bill, "number"),
    ("payment", Payment, "number"),
    ("journal_entry", JournalEntry, "ref"),
]

# Tables that have a status enum so we can split DRAFT vs POSTED.
# ``status`` is a string-or-enum column on each; we compare the value
# attribute when present, falling back to the raw value.
_STATUSED_ENTITIES = {"invoice", "bill", "journal_entry"}


def _status_str(row: Any) -> str:
    """Return the row's status as a plain str (handles StrEnum + str)."""
    val = getattr(row, "status", None)
    if val is None:
        return ""
    return getattr(val, "value", val)


def _label(row: Any, label_attr: str) -> str:
    val = getattr(row, label_attr, None)
    return str(val) if val is not None else "(unset)"


def _payload_for_log(row: Any) -> dict[str, Any]:
    """Minimal change_log payload — just enough to identify the row.

    We deliberately don't full-serialise via the per-entity ``_serialise``
    helpers: that's a coupling we don't need.  The change_log entry's
    purpose here is to document the cleanup action, not to provide a
    forensic before/after.  ``audit_snapshots`` would be the place for
    that, and we skip it intentionally for an ops cleanup.
    """
    out: dict[str, Any] = {
        "id": str(row.id),
        "company_id": str(row.company_id),
        "archived_at": (
            row.archived_at.isoformat() if row.archived_at is not None else None
        ),
        "version": row.version,
        "cleanup_reason": "cross_tenant_leak_default_tenant_junk",
    }
    # Add the label attr (name/number/ref) if present
    for attr in ("name", "number", "ref"):
        if hasattr(row, attr):
            v = getattr(row, attr)
            if v is not None:
                out[attr] = v
    if hasattr(row, "status"):
        out["status"] = _status_str(row)
    return out


async def _verify_company(session: AsyncSession) -> Company:
    """Return the Default Company row, or abort if it isn't what we expect.

    Guards against accidental wipe of a non-default company if the magic
    UUID is ever re-pointed.
    """
    company = await session.get(Company, DEFAULT_COMPANY_ID)
    if company is None:
        raise RuntimeError(
            f"Default Company {DEFAULT_COMPANY_ID} not found — refusing to run."
        )
    if company.name != DEFAULT_COMPANY_NAME:
        raise RuntimeError(
            f"Company {DEFAULT_COMPANY_ID} has name {company.name!r}, expected "
            f"{DEFAULT_COMPANY_NAME!r} — refusing to run."
        )
    if company.tenant_id != DEFAULT_TENANT_ID:
        raise RuntimeError(
            f"Company {DEFAULT_COMPANY_ID} has tenant_id {company.tenant_id}, "
            f"expected {DEFAULT_TENANT_ID} — refusing to run."
        )
    if company.archived_at is not None:
        raise RuntimeError(
            f"Default Company is already archived ({company.archived_at}) — "
            "refusing to run; cleanup may already be done."
        )
    return company


async def _fetch_candidates(
    session: AsyncSession,
    *,
    include_posted: bool,
) -> dict[str, list[Any]]:
    """Return non-archived rows in scope, keyed by entity name.

    With ``include_posted=False``, status-bearing tables filter to
    DRAFT only (POSTED rows are skipped).  Contacts and payments don't
    have a meaningful POSTED state for this purpose so are returned in
    full regardless.
    """
    out: dict[str, list[Any]] = {}
    for entity, model, _label_attr in _TABLES:
        stmt = (
            select(model)
            .where(
                model.company_id == DEFAULT_COMPANY_ID,
                model.archived_at.is_(None),
            )
            .order_by(model.created_at)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        if not include_posted and entity in _STATUSED_ENTITIES:
            rows = [r for r in rows if _status_str(r).upper() != "POSTED"]
        out[entity] = rows
    return out


def _print_dry_run(candidates: dict[str, list[Any]], *, include_posted: bool) -> None:
    """Human-readable dry-run report to stderr."""
    print(
        f"\n=== Cleanup target: Default Company {DEFAULT_COMPANY_ID} ===",
        file=sys.stderr,
    )
    print(f"include_posted = {include_posted}\n", file=sys.stderr)

    grand_total = 0
    for entity, _model, label_attr in _TABLES:
        rows = candidates.get(entity, [])
        grand_total += len(rows)
        if not rows:
            print(f"  {entity:14}    0 rows", file=sys.stderr)
            continue
        print(f"  {entity:14} {len(rows):4} rows:", file=sys.stderr)
        # Show every row up to 50; truncate beyond that.
        for r in rows[:50]:
            status_part = ""
            if entity in _STATUSED_ENTITIES:
                status_part = f"  [{_status_str(r):8}]"
            print(
                f"      {r.id}  {_label(r, label_attr):40}{status_part}",
                file=sys.stderr,
            )
        if len(rows) > 50:
            print(f"      … and {len(rows) - 50} more", file=sys.stderr)

    print(f"\nTOTAL rows that would be archived: {grand_total}\n", file=sys.stderr)


async def _archive_candidates(
    session: AsyncSession,
    candidates: dict[str, list[Any]],
) -> dict[str, int]:
    """Archive every row.  Caller controls commit/rollback.

    Returns a count map for the summary.  Does NOT commit.
    """
    counts: dict[str, int] = {}
    now = datetime.now(UTC)
    for entity, _model, _label_attr in _TABLES:
        rows = candidates.get(entity, [])
        for row in rows:
            row.archived_at = now
            row.version = row.version + 1
            await change_log_svc.append(
                session,
                entity=entity,
                entity_id=row.id,
                op="archive",
                actor=ACTOR,
                payload=_payload_for_log(row),
                version=row.version,
            )
        counts[entity] = len(rows)
    return counts


async def _verify_post_apply(session: AsyncSession) -> dict[str, int]:
    """After commit, count remaining non-archived rows under Default Company.

    With ``include_posted=True`` and a clean run, these counts should
    all be zero.  With ``include_posted=False``, the only remaining
    rows should be POSTED ones.
    """
    out: dict[str, int] = {}
    for entity, model, _label_attr in _TABLES:
        result = await session.execute(
            select(model).where(
                model.company_id == DEFAULT_COMPANY_ID,
                model.archived_at.is_(None),
            )
        )
        out[entity] = len(list(result.scalars().all()))
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Soft-archive critic-test junk in the Default Company "
            f"({DEFAULT_COMPANY_ID})."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually archive the rows.  Without this flag, the script only "
        "reports what it would do.",
    )
    parser.add_argument(
        "--include-posted",
        action="store_true",
        help="Also archive POSTED invoices/bills/journal_entries.  Default is "
        "DRAFT-only.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    async with AsyncSessionLocal() as session:
        try:
            await _verify_company(session)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        candidates = await _fetch_candidates(
            session, include_posted=args.include_posted
        )
        _print_dry_run(candidates, include_posted=args.include_posted)

        if not args.apply:
            print("Dry-run only.  Re-run with --apply to actually archive.",
                  file=sys.stderr)
            return 0

        # Apply path — wrap the archive in a single transaction so a
        # mid-flight failure rolls everything back.
        try:
            counts = await _archive_candidates(session, candidates)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Cleanup failed; transaction rolled back.")
            return 1

        print("\n=== Cleanup applied ===", file=sys.stderr)
        for entity, n in counts.items():
            print(f"  {entity:14} archived: {n}", file=sys.stderr)
        print(f"  TOTAL: {sum(counts.values())}", file=sys.stderr)

        # Re-query to verify the final state.
        remaining = await _verify_post_apply(session)
        print("\n=== Verification (rows still non-archived) ===", file=sys.stderr)
        for entity, n in remaining.items():
            print(f"  {entity:14} remaining: {n}", file=sys.stderr)
        if args.include_posted:
            unexpected = {k: v for k, v in remaining.items() if v > 0}
            if unexpected:
                print(
                    f"WARNING: with --include-posted, expected zero remaining "
                    f"in all tables; got {unexpected}",
                    file=sys.stderr,
                )
                return 3

        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
