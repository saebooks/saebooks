"""Tests for services.exports.company.build_company_export + routes."""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.user import User
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


async def _cleanup_user(username: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.username == username))
        await session.commit()


@pytest.fixture
async def unique_username() -> str:
    name = f"export-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        await _cleanup_user(name)


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


# ----- router -----


async def test_company_export_form_requires_accountant(
    client: AsyncClient, unique_username: str
) -> None:
    """readonly default role gets 403."""
    r = await client.get(
        "/admin/company/export",
        headers={"Remote-User": unique_username},
    )
    assert r.status_code == 403


async def test_company_export_form_renders_for_accountant(
    client: AsyncClient, unique_username: str
) -> None:
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        u.role = "accountant"
        await session.commit()

    r = await client.get(
        "/admin/company/export",
        headers={"Remote-User": unique_username},
    )
    assert r.status_code == 200
    assert "Download ZIP" in r.text


async def test_company_export_post_returns_zip(
    client: AsyncClient, unique_username: str
) -> None:
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        u.role = "accountant"
        await session.commit()

    company_id = await _seed_company_id()
    r = await client.post(
        "/admin/company/export",
        data={"company_id": str(company_id), "include_audit": "off"},
        headers={"Remote-User": unique_username},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers.get("content-disposition", "")
    # Round-trip the zip
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert any(n.endswith("company.json") for n in zf.namelist())
