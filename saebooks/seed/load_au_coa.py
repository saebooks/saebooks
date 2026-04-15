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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services.companies import ensure_seed_company

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


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in {"true", "1", "yes"}


async def _load_accounts(session: AsyncSession, company: Company) -> tuple[int, int]:
    csv_path = SEED_DIR / "account.account-au.csv"
    inserted = skipped = 0

    existing = await session.execute(
        select(Account.code).where(Account.company_id == company.id)
    )
    existing_codes = {code for (code,) in existing.all()}

    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            code = row["code"].strip()
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        inserted, skipped = await _load_accounts(session, company)
        logger.info(
            "Accounts for %s: %d inserted, %d already present", company.name, inserted, skipped
        )

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


if __name__ == "__main__":
    asyncio.run(main())
