"""Chart-of-accounts CSV round-trip.

Export format (what ``export_coa_csv`` emits) — 6 columns:

    code, name, account_type, parent_code, tax_code_default, reconcile

Codes are the hyphenated form stored in the DB (e.g. ``1-1110``).
``parent_code`` is resolved via the account's ``parent_id`` so the
import side can rebuild the tree. ``reconcile`` is a boolean emitted
as ``true``/``false`` to match the ABA/YAML conventions elsewhere.

Import is a two-step flow:

1. ``parse_coa_csv`` reads the CSV into ``CoaRow`` dataclasses.
2. ``diff_coa(existing_accounts, rows)`` returns a ``CoaDiff`` with
   three buckets: ``new`` (code not present), ``changed`` (fields
   differ from existing), ``removed`` (present in DB but missing from
   CSV — caller decides whether to archive).
3. ``apply_coa_diff`` actually persists the diff in a single
   transaction.

Header accounts are marked by setting ``parent_code`` empty AND
``code`` ending in ``-0000`` — matches the AU seed convention.
"""
from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType

CSV_HEADERS = ["code", "name", "account_type", "parent_code", "tax_code_default", "reconcile"]


@dataclass(frozen=True)
class CoaRow:
    """One parsed CoA row (either from CSV or from a DB account)."""

    code: str
    name: str
    account_type: AccountType
    parent_code: str | None
    tax_code_default: str | None
    reconcile: bool


@dataclass
class CoaDiff:
    """Diff between existing accounts and CSV rows."""

    new: list[CoaRow] = field(default_factory=list)
    changed: list[tuple[CoaRow, CoaRow]] = field(default_factory=list)  # (before, after)
    removed: list[CoaRow] = field(default_factory=list)
    unchanged: list[CoaRow] = field(default_factory=list)


class CoaImportError(ValueError):
    """Raised when a CSV row can't be parsed."""


def export_coa_csv(accounts: Sequence[Account]) -> str:
    """Render accounts as the 6-column CSV format.

    Rows are ordered by code so diffs are human-readable.
    """
    # Build a code lookup for parent_code resolution.
    by_id = {a.id: a for a in accounts}

    def _parent_code(acc: Account) -> str:
        parent = by_id.get(acc.parent_id) if acc.parent_id else None
        return parent.code if parent is not None else ""

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADERS)
    for acc in sorted(accounts, key=lambda a: a.code):
        w.writerow([
            acc.code,
            acc.name,
            acc.account_type.value,
            _parent_code(acc),
            acc.tax_code_default or "",
            "true" if acc.reconcile else "false",
        ])
    return buf.getvalue()


def parse_coa_csv(raw: str) -> list[CoaRow]:
    """Parse a CSV into ``CoaRow`` objects. Raises on bad rows."""
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise CoaImportError("CSV has no header row")

    lowered = {f.lower().strip(): f for f in reader.fieldnames}
    for required in ("code", "name", "account_type"):
        if required not in lowered:
            raise CoaImportError(f"missing required column: {required!r}")

    rows: list[CoaRow] = []
    for lineno, r in enumerate(reader, start=2):  # header = line 1
        code = (r.get(lowered["code"], "") or "").strip()
        name = (r.get(lowered["name"], "") or "").strip()
        raw_type = (r.get(lowered["account_type"], "") or "").strip().upper()
        if not code or not name or not raw_type:
            raise CoaImportError(
                f"line {lineno}: code, name and account_type are required"
            )
        try:
            acc_type = AccountType(raw_type)
        except ValueError as e:
            raise CoaImportError(
                f"line {lineno}: unknown account_type {raw_type!r}"
            ) from e
        parent_code_raw = r.get(lowered.get("parent_code", ""), "") or ""
        tax_code_raw = r.get(lowered.get("tax_code_default", ""), "") or ""
        reconcile_raw = (r.get(lowered.get("reconcile", ""), "") or "").strip().lower()
        rows.append(
            CoaRow(
                code=code,
                name=name,
                account_type=acc_type,
                parent_code=parent_code_raw.strip() or None,
                tax_code_default=tax_code_raw.strip() or None,
                reconcile=reconcile_raw in ("true", "1", "yes", "y", "t"),
            )
        )
    return rows


def diff_coa(existing: Sequence[Account], rows: Iterable[CoaRow]) -> CoaDiff:
    """Classify each row as new / changed / unchanged, plus the removed set."""
    existing_by_code = {a.code: _account_to_row(a, existing) for a in existing}
    diff = CoaDiff()
    seen: set[str] = set()

    for row in rows:
        seen.add(row.code)
        before = existing_by_code.get(row.code)
        if before is None:
            diff.new.append(row)
        elif before == row:
            diff.unchanged.append(row)
        else:
            diff.changed.append((before, row))

    for code, before in existing_by_code.items():
        if code not in seen:
            diff.removed.append(before)
    return diff


def _account_to_row(acc: Account, accounts: Sequence[Account]) -> CoaRow:
    by_id = {a.id: a for a in accounts}
    parent = by_id.get(acc.parent_id) if acc.parent_id else None
    return CoaRow(
        code=acc.code,
        name=acc.name,
        account_type=acc.account_type,
        parent_code=parent.code if parent is not None else None,
        tax_code_default=acc.tax_code_default,
        reconcile=acc.reconcile,
    )


async def apply_coa_diff(
    session: AsyncSession,
    company_id: uuid.UUID,
    diff: CoaDiff,
    *,
    archive_removed: bool = False,
) -> dict[str, int]:
    """Apply the diff. Returns counts of each bucket actually applied.

    ``archive_removed=True`` soft-deletes accounts that are missing from
    the CSV; by default they are left alone (safer — a short CSV
    shouldn't nuke the CoA).
    """
    # Load everything up-front so parent_code resolution is O(1).
    all_accounts = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()
    by_code = {a.code: a for a in all_accounts}

    applied = {"new": 0, "changed": 0, "archived": 0}

    # Pass 1: create rows (without parent resolution so parents can be
    # created in the same batch).
    for row in diff.new:
        new_acc = Account(
            company_id=company_id,
            code=row.code,
            name=row.name,
            account_type=row.account_type,
            tax_code_default=row.tax_code_default,
            reconcile=row.reconcile,
        )
        session.add(new_acc)
        by_code[row.code] = new_acc
        applied["new"] += 1
    await session.flush()

    # Pass 2: resolve parents for newly-created + changed rows.
    for row in diff.new:
        if row.parent_code:
            parent = by_code.get(row.parent_code)
            if parent is not None:
                by_code[row.code].parent_id = parent.id

    for before, after in diff.changed:
        acc = by_code.get(before.code)
        if acc is None:
            continue
        acc.name = after.name
        acc.account_type = after.account_type
        acc.tax_code_default = after.tax_code_default
        acc.reconcile = after.reconcile
        if after.parent_code:
            parent = by_code.get(after.parent_code)
            acc.parent_id = parent.id if parent is not None else None
        else:
            acc.parent_id = None
        applied["changed"] += 1

    if archive_removed:
        now = datetime.now(UTC)
        for row in diff.removed:
            acc = by_code.get(row.code)
            if acc is not None and acc.archived_at is None:
                acc.archived_at = now
                applied["archived"] += 1

    await session.flush()
    return applied


__all__ = [
    "CSV_HEADERS",
    "CoaDiff",
    "CoaImportError",
    "CoaRow",
    "apply_coa_diff",
    "diff_coa",
    "export_coa_csv",
    "parse_coa_csv",
]
