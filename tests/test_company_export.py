"""Tests for services.exports.company.build_company_export.

Cat-C rollup dropped the legacy /admin/company/export HTML form router; the
service-level tests below remain as the contract for the export builder.
The replacement v1 surface is /api/v1/companies (export not yet ported).
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services.exports.company import build_company_export


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


# ----- service -----


async def test_build_company_export_zip_shape() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        payload, filename = await build_company_export(
            session,
            company_id=company_id,
            exported_by="pytest",
            include_audit=False,
        )

    assert filename.startswith(f"company-{company_id}-")
    assert filename.endswith(".zip")

    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = zf.namelist()
    root = f"company-{company_id}-"
    expected_suffixes = {
        "company.json",
        "contacts.json",
        "accounts.json",
        "tax_codes.json",
        "journal_entries.json",
        "invoices.json",
        "bills.json",
        "credit_notes.json",
        "payments.json",
        "fixed_assets.json",
        "bank_feed_accounts.json",
        "bank_statement_lines.json",
        "README.txt",
    }
    for suffix in expected_suffixes:
        assert any(
            n.startswith(root) and n.endswith(suffix) for n in names
        ), f"missing {suffix} in {names}"
    # audit.csv excluded when include_audit=False
    assert not any(n.endswith("audit.csv") for n in names)


async def test_build_company_export_with_audit() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        payload, _ = await build_company_export(
            session, company_id=company_id, include_audit=True
        )
    zf = zipfile.ZipFile(io.BytesIO(payload))
    assert any(n.endswith("audit.csv") for n in zf.namelist())


async def test_build_company_export_company_json_has_company_id() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        payload, _ = await build_company_export(
            session, company_id=company_id, include_audit=False
        )

    zf = zipfile.ZipFile(io.BytesIO(payload))
    company_json_name = next(n for n in zf.namelist() if n.endswith("company.json"))
    data = json.loads(zf.read(company_json_name))
    assert data["id"] == str(company_id)


async def test_build_company_export_unknown_company_raises() -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="not found"):
            await build_company_export(session, company_id=uuid.uuid4())


async def test_build_company_export_accounts_sorted_by_code() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        payload, _ = await build_company_export(
            session, company_id=company_id, include_audit=False
        )
    zf = zipfile.ZipFile(io.BytesIO(payload))
    name = next(n for n in zf.namelist() if n.endswith("accounts.json"))
    rows = json.loads(zf.read(name))
    codes = [r["code"] for r in rows]
    assert codes == sorted(codes)


async def test_build_company_export_readme_lists_counts() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        payload, _ = await build_company_export(
            session, company_id=company_id, include_audit=False
        )
    zf = zipfile.ZipFile(io.BytesIO(payload))
    name = next(n for n in zf.namelist() if n.endswith("README.txt"))
    text = zf.read(name).decode("utf-8")
    assert "SAE Books" in text
    assert "accounts" in text


# NOTE: HTML form router tests for /admin/company/export deleted with the
# Cat-C rollup. Re-add HTTP-level tests against /api/v1/companies/{id}/export
# when that endpoint lands.
