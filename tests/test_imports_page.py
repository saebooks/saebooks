"""Router smoke tests for /admin/imports/..."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.fixture
async def client(admin_client: AsyncClient) -> AsyncClient:
    """All ``/admin/imports/*`` routes are gated by ``require_role(ADMIN)``;
    delegate the file-local ``client`` to the conftest ``admin_client``."""
    return admin_client


@pytest.mark.asyncio
async def test_imports_index_renders(client: AsyncClient) -> None:
    r = await client.get("/admin/imports")
    assert r.status_code == 200
    assert "Imports" in r.text
    assert "/admin/imports/bank" in r.text
    assert "/admin/imports/coa" in r.text
    assert "/admin/imports/qbo" in r.text


@pytest.mark.asyncio
async def test_bank_index_renders(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/bank")
    assert r.status_code == 200
    assert "Import bank statement" in r.text


@pytest.mark.asyncio
async def test_coa_index_renders(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/coa")
    assert r.status_code == 200
    assert "Chart of accounts" in r.text
    assert "Download" in r.text


@pytest.mark.asyncio
async def test_coa_export_returns_csv(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/coa/export?download=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "code,name,account_type,parent_code,tax_code_default,reconcile" in r.text
    # First AU seed bank account's hyphenated code.
    assert "1-1110" in r.text


@pytest.mark.asyncio
async def test_coa_preview_accepts_csv(client: AsyncClient) -> None:
    raw = (
        "code,name,account_type,parent_code,tax_code_default,reconcile\n"
        "9-9999,Z Test New Account,EXPENSE,,,false\n"
    )
    files = {
        "file": ("new.csv", raw.encode("utf-8"), "text/csv"),
    }
    r = await client.post("/admin/imports/coa/preview", files=files)
    assert r.status_code == 200
    assert "diff" in r.text.lower()
    assert "Z Test New Account" in r.text


@pytest.mark.asyncio
async def test_qbo_index_renders(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/qbo")
    assert r.status_code == 200
    assert "QuickBooks" in r.text


@pytest.mark.asyncio
async def test_qbo_contacts_preview(client: AsyncClient) -> None:
    raw = (
        "Customer,Email,Billing City,Billing State,Billing Zip\n"
        "Acme Corp,a@example.com,Brisbane,QLD,4000\n"
    )
    r = await client.post(
        "/admin/imports/qbo/contacts/preview",
        data={"kind": "customer"},
        files={"file": ("customers.csv", raw.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200
    assert "Acme Corp" in r.text
