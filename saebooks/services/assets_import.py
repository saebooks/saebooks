"""Asset-register CSV bulk import (Batch MM/2).

Two-step flow mirroring ``saebooks.services.imports.coa``:

1. ``parse_assets_csv(raw)`` — pure parse + validate; returns a list of
   ``AssetImportRow`` dataclasses with line-numbered errors attached.
2. ``classify_rows(session, company_id, rows)`` — classify each parsed
   row against the live DB (skip / create / invalid) without writing
   anything.
3. ``apply_import(session, company_id, plan)`` — persist the ``create``
   bucket in one transaction.

Idempotent on ``(company_id, code)`` — re-running the same CSV twice
adds zero new rows. Useful for customer migrations off Xero/QBO where
the asset export gets re-downloaded during iteration.

CSV columns (header row is case-insensitive; whitespace tolerated):

    Required: code, name, purchase_date, cost, depreciation_model_id,
              cost_account_code, accum_dep_account_code
    Optional: in_service_date, residual_value, dep_expense_account_code,
              description, serial_number, manufacturer, model_number,
              location, custody_person, warranty_end

Account codes (``cost_account_code`` etc.) are the hyphenated form
stored in the DB — e.g. ``1-3310``. They resolve to UUIDs at apply
time; if the code doesn't exist on this company, the row is flagged
invalid with a line-numbered error rather than silently dropped.

Depreciation model IDs are the catalogue slugs (``asset_5_year_linear``,
``asset_dv_30``, etc.).
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.depreciation_model import DepreciationModel
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import assets as svc

_REQUIRED_COLUMNS = (
    "code",
    "name",
    "purchase_date",
    "cost",
    "depreciation_model_id",
    "cost_account_code",
    "accum_dep_account_code",
)

# Default dep-expense account code. Matches the AU seed default on the
# service-layer create() path; CSV rows can still override per-row.
_DEFAULT_DEP_EXPENSE_CODE = "6-1500"


@dataclass(frozen=True)
class AssetImportRow:
    """One parsed CSV row. ``errors`` populated during parse/classify."""

    lineno: int
    code: str
    name: str
    purchase_date: date | None
    in_service_date: date | None
    cost: Decimal | None
    residual_value: Decimal | None
    depreciation_model_id: str
    cost_account_code: str
    accum_dep_account_code: str
    dep_expense_account_code: str | None
    description: str | None
    serial_number: str | None
    manufacturer: str | None
    model_number: str | None
    location: str | None
    custody_person: str | None
    warranty_end: date | None
    errors: tuple[str, ...] = ()


@dataclass
class ImportPlan:
    """Classification buckets + the rows themselves.

    ``create`` — rows with no existing FixedAsset for this ``(company, code)``.
    ``skip``   — rows where ``code`` already exists; idempotent pass-through.
    ``invalid``— parse or DB-resolution failures; never written.
    """

    create: list[AssetImportRow] = field(default_factory=list)
    skip: list[AssetImportRow] = field(default_factory=list)
    invalid: list[AssetImportRow] = field(default_factory=list)


class AssetImportError(ValueError):
    """Raised on header-level CSV problems (missing columns etc.)."""


def _parse_decimal(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    # Try a few common AU formats. The CoA import and bank-CSV import
    # both accept dd/mm/yyyy + ISO; mirror that here.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return date.fromisoformat(raw) if fmt == "%Y-%m-%d" else date(
                *reversed([int(x) for x in raw.replace("-", "/").split("/")])
            )
        except (ValueError, TypeError):
            continue
    return None


def _nz(raw: str | None) -> str | None:
    """Stripped-or-None — empty-string/None both collapse to None."""
    if raw is None:
        return None
    s = raw.strip()
    return s if s else None


def parse_assets_csv(raw: str) -> list[AssetImportRow]:
    """Parse the CSV into ``AssetImportRow``s with row-level errors.

    Raises ``AssetImportError`` only for header-level failures (missing
    columns, empty header). Per-row problems (blank required field,
    unparseable date, bad decimal) are captured as ``errors`` on the
    row itself — the caller decides whether to continue or abort.
    """
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise AssetImportError("CSV has no header row")

    lowered = {f.lower().strip(): f for f in reader.fieldnames}
    for required in _REQUIRED_COLUMNS:
        if required not in lowered:
            raise AssetImportError(f"missing required column: {required!r}")

    rows: list[AssetImportRow] = []
    for lineno, r in enumerate(reader, start=2):  # header is line 1
        errors: list[str] = []

        # Default-arg capture of ``r`` + ``lowered`` silences B023 (the
        # closure is only called within this iteration, but ruff can't
        # see that — make the binding explicit).
        def _get(col: str, *, _r: dict[str, str] = r, _lowered: dict[str, str] = lowered) -> str:
            return (_r.get(_lowered.get(col, ""), "") or "").strip()

        code = _get("code")
        name = _get("name")
        if not code:
            errors.append("code is required")
        if not name:
            errors.append("name is required")

        purchase_date = _parse_date(_get("purchase_date"))
        if _get("purchase_date") and purchase_date is None:
            errors.append(f"invalid purchase_date {_get('purchase_date')!r}")
        if purchase_date is None and not errors:
            errors.append("purchase_date is required")

        in_service_date = _parse_date(_get("in_service_date"))
        if _get("in_service_date") and in_service_date is None:
            errors.append(
                f"invalid in_service_date {_get('in_service_date')!r}"
            )
        warranty_end = _parse_date(_get("warranty_end"))
        if _get("warranty_end") and warranty_end is None:
            errors.append(f"invalid warranty_end {_get('warranty_end')!r}")

        cost = _parse_decimal(_get("cost"))
        if _get("cost") and cost is None:
            errors.append(f"invalid cost {_get('cost')!r}")
        if cost is None and not errors:
            errors.append("cost is required")
        if cost is not None and cost <= 0:
            errors.append("cost must be > 0")

        residual = _parse_decimal(_get("residual_value"))
        if _get("residual_value") and residual is None:
            errors.append(
                f"invalid residual_value {_get('residual_value')!r}"
            )

        depreciation_model_id = _get("depreciation_model_id")
        if not depreciation_model_id:
            errors.append("depreciation_model_id is required")

        cost_account_code = _get("cost_account_code")
        if not cost_account_code:
            errors.append("cost_account_code is required")

        accum_dep_account_code = _get("accum_dep_account_code")
        if not accum_dep_account_code:
            errors.append("accum_dep_account_code is required")

        rows.append(
            AssetImportRow(
                lineno=lineno,
                code=code,
                name=name,
                purchase_date=purchase_date,
                in_service_date=in_service_date or purchase_date,
                cost=cost,
                residual_value=residual,
                depreciation_model_id=depreciation_model_id,
                cost_account_code=cost_account_code,
                accum_dep_account_code=accum_dep_account_code,
                dep_expense_account_code=_nz(_get("dep_expense_account_code")),
                description=_nz(_get("description")),
                serial_number=_nz(_get("serial_number")),
                manufacturer=_nz(_get("manufacturer")),
                model_number=_nz(_get("model_number")),
                location=_nz(_get("location")),
                custody_person=_nz(_get("custody_person")),
                warranty_end=warranty_end,
                errors=tuple(errors),
            )
        )
    return rows


async def classify_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    rows: list[AssetImportRow],
) -> ImportPlan:
    """Classify rows into create/skip/invalid against the live DB.

    Pre-loads existing asset codes + the depreciation catalogue + all
    non-archived accounts for this company so resolution is O(row) in
    Python memory rather than a DB hit per row.
    """
    plan = ImportPlan()

    # Load lookup tables once.
    existing_codes = set(
        (
            await session.execute(
                select(FixedAsset.code).where(
                    FixedAsset.company_id == company_id,
                )
            )
        ).scalars().all()
    )
    model_ids = set(
        (
            await session.execute(select(DepreciationModel.id))
        ).scalars().all()
    )
    accounts_by_code = {
        a.code: a
        for a in (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.archived_at.is_(None),
                )
            )
        ).scalars().all()
    }

    for row in rows:
        errors = list(row.errors)

        if row.code in existing_codes:
            plan.skip.append(row)
            continue

        # DB-resolution checks (these only matter if the row wasn't
        # already a parse failure).
        if not errors:
            if row.depreciation_model_id not in model_ids:
                errors.append(
                    f"depreciation_model_id {row.depreciation_model_id!r} "
                    f"not in catalogue"
                )
            if row.cost_account_code not in accounts_by_code:
                errors.append(
                    f"cost_account_code {row.cost_account_code!r} not found"
                )
            if row.accum_dep_account_code not in accounts_by_code:
                errors.append(
                    f"accum_dep_account_code {row.accum_dep_account_code!r} "
                    f"not found"
                )
            dep_code = (
                row.dep_expense_account_code or _DEFAULT_DEP_EXPENSE_CODE
            )
            if dep_code not in accounts_by_code:
                errors.append(
                    f"dep_expense_account_code {dep_code!r} not found"
                )

        if errors:
            plan.invalid.append(
                AssetImportRow(**{**row.__dict__, "errors": tuple(errors)})
            )
        else:
            plan.create.append(row)

    return plan


async def apply_import(
    session: AsyncSession,
    company_id: uuid.UUID,
    plan: ImportPlan,
) -> int:
    """Persist the ``create`` bucket. Returns the number of rows written.

    Resolves account-code → account-id once up-front. Uses the live
    ``services.assets.create`` so all downstream invariants (auto-code
    on blank, residual default, in-service fallback) apply uniformly
    to CSV imports and manual creates.
    """
    if not plan.create:
        return 0

    accounts_by_code = {
        a.code: a
        for a in (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.archived_at.is_(None),
                )
            )
        ).scalars().all()
    }

    written = 0
    for row in plan.create:
        assert row.cost is not None  # classified-valid rows always have cost
        assert row.purchase_date is not None

        dep_code = row.dep_expense_account_code or _DEFAULT_DEP_EXPENSE_CODE
        await svc.create(
            session,
            company_id,
            code=row.code or None,
            name=row.name,
            description=row.description,
            cost_account_id=accounts_by_code[row.cost_account_code].id,
            accum_dep_account_id=accounts_by_code[
                row.accum_dep_account_code
            ].id,
            dep_expense_account_id=accounts_by_code[dep_code].id,
            depreciation_model_id=row.depreciation_model_id,
            purchase_date=row.purchase_date,
            in_service_date=row.in_service_date,
            cost=row.cost,
            residual_value=row.residual_value,
            serial_number=row.serial_number,
            manufacturer=row.manufacturer,
            model_number=row.model_number,
            location=row.location,
            custody_person=row.custody_person,
            warranty_end=row.warranty_end,
        )
        written += 1
    return written


__all__ = [
    "AssetImportError",
    "AssetImportRow",
    "ImportPlan",
    "apply_import",
    "classify_rows",
    "parse_assets_csv",
]
