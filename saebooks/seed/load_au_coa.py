"""Load the Odoo l10n_au chart of accounts into the default company.

Idempotent — re-running inserts missing rows but doesn't duplicate.

Run: `docker compose exec app python -m saebooks.seed.load_au_coa`
"""
import asyncio
import csv
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import LoginSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services.companies import ensure_seed_company
from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes
from saebooks.services.tax_codes import (
    ensure_international_seed as ensure_intl_tax_codes,
)

logger = logging.getLogger("saebooks.seed.au_coa")

SEED_DIR = Path(__file__).parent / "au"

ODOO_TYPE_MAP: dict[str, AccountType] = {
    "asset_cash": AccountType.ASSET,
    "asset_current": AccountType.ASSET,
    "asset_non_current": AccountType.ASSET,
    "asset_receivable": AccountType.ASSET,
    "asset_prepayments": AccountType.ASSET,
    "asset_fixed": AccountType.ASSET,
    "liability_current": AccountType.LIABILITY,
    "liability_non_current": AccountType.LIABILITY,
    "liability_payable": AccountType.LIABILITY,
    "liability_credit_card": AccountType.LIABILITY,
    "equity": AccountType.EQUITY,
    "equity_unaffected": AccountType.EQUITY,
    "income": AccountType.INCOME,
    "income_other": AccountType.OTHER_INCOME,
    "expense": AccountType.EXPENSE,
    "expense_depreciation": AccountType.EXPENSE,
    "expense_direct_cost": AccountType.COST_OF_SALES,
}


# Generic placeholder accounts from Odoo that add no value — skip on seed
_SKIP_CODES = {"41110", "41120", "41130", "51110", "51120", "51130"}

# Classic-mode prefix → first digit for hyphenation
# Odoo CSV has flat codes like "11110"; we store as "1-1110"
_CLASSIC_PREFIXES = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]


def _hyphenate_code(code: str) -> str:
    """Convert flat Odoo code to hyphenated format.

    Finds the range prefix (longest match from _CLASSIC_PREFIXES)
    and inserts a hyphen. E.g. "11110" → "1-1110", "81000" → "8-1000".
    """
    for prefix in sorted(_CLASSIC_PREFIXES, key=len, reverse=True):
        if code.startswith(prefix):
            return f"{prefix}-{code[len(prefix):]}"
    return code  # fallback — shouldn't happen


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in {"true", "1", "yes"}


# Sub-header accounts to create before the CSV seed
# These provide proper hierarchy grouping in the account list
_SUB_HEADERS: list[tuple[str, str, AccountType]] = [
    ("1-1000", "Current Assets", AccountType.ASSET),
    ("1-1100", "Cash & Bank", AccountType.ASSET),
    # NB: NO "1-1200 Receivables" group header. Code 1-1200 is the postable
    # "Trade Debtors" AR control account in the CSV (au_11200) and is hard-coded
    # as the AR control code across the services (invoices/payments/credit_notes/
    # edition/fx). Seeding a header here first masks the CSV leaf (same code is
    # skipped as a dup), leaving 1-1200 a header — then NO invoice/payment can
    # post (PostingError: cannot post to header). Production escaped this only
    # because its Trade Debtors leaf predates migration 0012. Keep 1-1200 a leaf.
    ("1-1300", "Inventory", AccountType.ASSET),
    ("1-2000", "Prepayments & Deposits", AccountType.ASSET),
    ("1-3000", "Property, Plant & Equipment", AccountType.ASSET),
    ("2-1000", "Current Liabilities", AccountType.LIABILITY),
    ("2-2000", "Non-Current Liabilities", AccountType.LIABILITY),
]

# SAE-specific CoA additions not in the upstream Odoo CSV.
# Codes are stored pre-hyphenated (no _hyphenate_code conversion needed).
# (code, name, account_type)
_EXTRA_ACCOUNTS: list[tuple[str, str, AccountType]] = [
    # Splits generic 6-2420 Superannuation into two variants so tax attribution
    # is possible: employer SG (BAS W2) vs contractor SMSF contributions
    # (individual/SMSF tax return line). Both are expense-type accounts.
    ("6-2420-SG", "Superannuation - Employer SG", AccountType.EXPENSE),
    ("6-2420-SMSF", "Superannuation - Contractor Self-Managed (SMSF)", AccountType.EXPENSE),
    # Payroll clearing account — Cr side of pay-run ABA journal; cleared when
    # payment hits the bank feed and is matched to the ABA disbursement.
    ("2-1150", "Payments — Pending", AccountType.LIABILITY),
]


async def _load_accounts(session: AsyncSession, company: Company) -> tuple[int, int]:
    csv_path = SEED_DIR / "account.account-au.csv"
    inserted = skipped = 0

    existing = await session.execute(
        select(Account.code).where(Account.company_id == company.id)
    )
    existing_codes = {code for (code,) in existing.all()}

    # Insert sub-header accounts first
    for code, name, acct_type in _SUB_HEADERS:
        if code in existing_codes:
            continue
        session.add(
            Account(
                company_id=company.id,
                code=code,
                name=name,
                account_type=acct_type,
                is_header=True,
            )
        )
        existing_codes.add(code)
        inserted += 1

    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            raw_code = row["code"].strip()
            if raw_code in _SKIP_CODES:
                skipped += 1
                continue
            code = _hyphenate_code(raw_code)
            if code in existing_codes:
                skipped += 1
                continue
            odoo_type = row["account_type"].strip()
            mapped = ODOO_TYPE_MAP.get(odoo_type)
            if mapped is None:
                raise ValueError(f"Unmapped Odoo account_type {odoo_type!r} on code {code}")
            session.add(
                Account(
                    company_id=company.id,
                    code=code,
                    name=row["name"].strip(),
                    account_type=mapped,
                    reconcile=_parse_bool(row.get("reconcile", "")),
                    extra={
                        "odoo_id": row.get("id"),
                        "odoo_account_type": odoo_type,
                        "tag_ids": row.get("tag_ids", ""),
                        "tax_ids": row.get("tax_ids", ""),
                        "non_trade": row.get("non_trade", ""),
                        "depreciation_model_id": row.get("depreciation_model_id", ""),
                        "description": row.get("description", ""),
                    },
                )
            )
            inserted += 1

    # SAE-specific additions (pre-hyphenated codes, not in the Odoo CSV)
    for code, name, acct_type in _EXTRA_ACCOUNTS:
        if code in existing_codes:
            skipped += 1
            continue
        session.add(
            Account(
                company_id=company.id,
                code=code,
                name=name,
                account_type=acct_type,
            )
        )
        existing_codes.add(code)
        inserted += 1

    await session.commit()
    return inserted, skipped


async def _load_raw(session: AsyncSession, table: str, csv_name: str) -> int:
    from sqlalchemy import text

    path = SEED_DIR / csv_name
    if not path.exists():
        return 0

    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    n = 0
    for row in rows:
        row_id = row.get("id") or row.get("code") or ""
        if not row_id:
            continue
        await session.execute(
            text(
                f"INSERT INTO {table} (id, data) VALUES (:id, CAST(:data AS jsonb)) "
                f"ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data"
            ).bindparams(id=row_id, data=json.dumps(row)),
        )
        n += 1
    await session.commit()
    return n


async def _load_depreciation_models(session: AsyncSession) -> int:
    """Populate the ``depreciation_models`` catalogue from the AU CSV.

    This is the proper typed table (one row per schedule), not the raw
    JSONB shadow. Idempotent via ``ON CONFLICT (id) DO NOTHING`` — slugs
    are stable, so re-running the seed is a no-op.
    """
    from sqlalchemy import text

    path = SEED_DIR / "account.depreciation.model-au.csv"
    if not path.exists():
        return 0

    n = 0
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            slug = row.get("id", "").strip()
            if not slug:
                continue
            method = row.get("method", "").strip()
            method_number = int(row.get("method_number", "0") or "0")
            method_period = int(row.get("method_period", "0") or "0")
            raw_factor = row.get("method_progress_factor", "").strip()
            progress_factor = raw_factor if raw_factor else None
            raw_rate = row.get("rate_pct", "").strip()
            # Cast empty strings to SQL NULL via ``::numeric`` — asyncpg
            # otherwise sends bare strings as VARCHAR and mismatches the
            # numeric column.
            await session.execute(
                text(
                    "INSERT INTO depreciation_models ("
                    "id, method, method_number, method_period, "
                    "method_progress_factor, rate_pct"
                    ") VALUES (:id, :method, :mnum, :mper, "
                    "CAST(:mpf AS numeric), CAST(:rate AS numeric)) "
                    "ON CONFLICT (id) DO NOTHING"
                ).bindparams(
                    id=slug,
                    method=method,
                    mnum=method_number,
                    mper=method_period,
                    mpf=progress_factor,
                    rate=raw_rate if raw_rate else None,
                ),
            )
            n += 1
    await session.commit()
    return n


async def _seed_period_locks(session: AsyncSession, company: Company) -> int:
    """Idempotently seed period locks for closed quarters.

    Seeds a Q1 2026 lock (through 2026-03-31) representing the first BAS
    quarter that should be closed on a fresh install. Any existing lock on
    that same date is skipped.
    """
    from datetime import date as _date

    from sqlalchemy import select

    from saebooks.models.journal import PeriodLock

    q1_lock_date = _date(2026, 3, 31)
    existing = await session.execute(
        select(PeriodLock).where(
            PeriodLock.company_id == company.id,
            PeriodLock.locked_through == q1_lock_date,
        )
    )
    # Use ``.first()`` rather than ``scalar_one_or_none()``: the table has
    # no UNIQUE(company_id, locked_through) constraint, so historical drift
    # could leave duplicate seed rows. Idempotent seeding only needs to know
    # whether ANY row exists, not exactly one.
    if existing.scalars().first() is not None:
        return 0

    session.add(
        PeriodLock(
            company_id=company.id,
            locked_through=q1_lock_date,
            locked_by="seed",
            reason="Q1 2026 BAS quarter closed (initial seed)",
        )
    )
    await session.commit()
    return 1


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with LoginSessionLocal() as session:
        company = await ensure_seed_company(session)
        tax_inserted = await ensure_tax_codes(session, company.id)
        logger.info("Tax codes: %d inserted", tax_inserted)
        intl_inserted = await ensure_intl_tax_codes(session, company.id)
        logger.info("International tax codes: %d inserted", intl_inserted)
        inserted, skipped = await _load_accounts(session, company)
        logger.info(
            "Accounts for %s: %d inserted, %d already present", company.name, inserted, skipped
        )

        locks_seeded = await _seed_period_locks(session, company)
        logger.info("Period locks seeded: %d", locks_seeded)

        # The raw_* JSONB staging tables and the typed depreciation_models
        # catalogue are populated with Postgres-only DML (``CAST(... AS jsonb)``,
        # ``ON CONFLICT``) against tables that live in the reference/Postgres
        # schema, not the ORM metadata that ``bootstrap_schema`` builds. On the
        # SQLite Cashbook backend (single-device community / mobile) those
        # tables don't exist and the syntax isn't portable — conftest documents
        # the same limitation and skips this seed on SQLite. Everything the core
        # double-entry engine needs (company, tax codes, chart of accounts,
        # period locks) is already loaded above via reference-free ORM inserts,
        # so we simply skip the Postgres-only reference-data load on SQLite
        # rather than crash the seed (and, in a container, the whole boot).
        is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
        if is_sqlite:
            logger.info(
                "SQLite backend — skipping Postgres-only reference-data load "
                "(raw_* staging tables + depreciation_models catalogue)."
            )
        else:
            raw_sources = [
                ("raw_au_tax_codes", "account.tax-au.csv"),
                ("raw_au_tax_groups", "account.tax.group-au.csv"),
                ("raw_au_fiscal_positions", "account.fiscal.position-au.csv"),
                ("raw_au_account_tags", "account.account.tag.csv"),
                ("raw_au_depreciation_models", "account.depreciation.model-au.csv"),
            ]
            for table, name in raw_sources:
                n = await _load_raw(session, table, name)
                logger.info("  %s: %d rows", table, n)

            dep_models = await _load_depreciation_models(session)
            logger.info("  depreciation_models (typed): %d rows", dep_models)


if __name__ == "__main__":
    asyncio.run(main())
