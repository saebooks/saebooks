"""Router tests for /admin/imports/* (imports.py).

Covers:
* GET /admin/imports/ → 200
* GET /admin/imports/bank → 200 (account list rendered)
* POST /admin/imports/bank/preview with valid CSV → 200, preview shown
* POST /admin/imports/bank/preview with OFX snippet → 200, fmt_label OFX
* POST /admin/imports/bank/preview with bad CSV → 400 (error shown)
* POST /admin/imports/bank/preview unknown account_id → 400
* POST /admin/imports/bank/apply with valid CSV → 200, inserted count shown
* GET /admin/imports/coa → 200
* GET /admin/imports/coa/export → 200, text/csv
* GET /admin/imports/coa/export?download=1 → Content-Disposition attachment
* POST /admin/imports/coa/preview with valid CSV → 200
* POST /admin/imports/coa/preview with bad CSV → 400
* POST /admin/imports/coa/apply with valid CSV → redirect
* GET /admin/imports/qbo → 200
* POST /admin/imports/qbo/contacts/preview with valid CSV → 200
* POST /admin/imports/qbo/contacts/preview with bad CSV → 400
* POST /admin/imports/qbo/contacts/apply with valid CSV → redirect
"""
from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company


@pytest.fixture
async def client(admin_client: AsyncClient) -> AsyncClient:
    """All ``/admin/imports/*`` routes are gated by ``require_role(ADMIN)``;
    delegate the file-local ``client`` to the conftest ``admin_client``."""
    return admin_client


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None, "Test DB has no active company"
    return company


@pytest.fixture
async def reconcile_account() -> Account:
    """Return (or create) a reconcilable ASSET account for bank import tests."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.ASSET,
                    Account.reconcile.is_(True),
                    Account.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if existing:
            return existing
        # Create one if none exist
        acct = Account(
            company_id=company.id,
            code=f"1{uuid.uuid4().int % 9000:04d}",
            name="Test Bank Import Account",
            account_type=AccountType.ASSET,
            reconcile=True,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
    return acct


# Minimal generic CSV that the bank importer will accept
_GENERIC_CSV = (
    "date,amount,description\n"
    "2026-04-01,100.00,Opening balance\n"
    "2026-04-02,-45.50,Test expense\n"
)

_OFX_SNIPPET = (
    "OFXHEADER:100\n"
    "DATA:OFXSGML\n"
    "VERSION:102\n"
    "SECURITY:NONE\n"
    "ENCODING:USASCII\n"
    "CHARSET:1252\n"
    "COMPRESSION:NONE\n"
    "OLDFILEUID:NONE\n"
    "NEWFILEUID:NONE\n"
    "<OFX>\n"
    "<BANKMSGSRSV1>\n"
    "<STMTTRNRS>\n"
    "<TRNUID>1001\n"
    "<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
    "<STMTRS>\n"
    "<CURDEF>AUD\n"
    "<BANKACCTFROM><BANKID>062000<ACCTID>123456789<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
    "<BANKTRANLIST>\n"
    "<DTSTART>20260401\n"
    "<DTEND>20260430\n"
    "<STMTTRN>\n"
    "<TRNTYPE>CREDIT\n"
    "<DTPOSTED>20260401\n"
    "<TRNAMT>100.00\n"
    "<FITID>TXN001\n"
    "<NAME>Test income\n"
    "</STMTTRN>\n"
    "</BANKTRANLIST>\n"
    "<LEDGERBAL><BALAMT>100.00<DTASOF>20260430</LEDGERBAL>\n"
    "</STMTRS>\n"
    "</STMTTRNRS>\n"
    "</BANKMSGSRSV1>\n"
    "</OFX>\n"
)

# Minimal COA CSV (header + one row)
_COA_CSV = (
    "code,name,type,parent_code,tax_code_default,reconcile\n"
    "9999,Test Import Account,EXPENSE,,GST,false\n"
)

# QBO contacts CSV (customer export shape)
_QBO_CONTACTS_CSV = (
    "Customer,Company,First Name,Last Name,Email,Phone,"
    "Billing Address Line 1,Billing City,Billing State,Billing Zip\n"
    "Test Customer Co,,Test,Customer,test@example.com,0400000000,"
    "1 Test St,Testville,NSW,2000\n"
)


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


async def test_imports_index_200(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/")
    assert r.status_code == 200


async def test_imports_index_no_trailing_slash_200(client: AsyncClient) -> None:
    r = await client.get("/admin/imports")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Bank statements
# ---------------------------------------------------------------------------


async def test_bank_index_200(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/bank")
    assert r.status_code == 200


async def test_bank_preview_valid_csv_200(
    client: AsyncClient, reconcile_account: Account
) -> None:
    r = await client.post(
        "/admin/imports/bank/preview",
        files={"file": ("test.csv", io.BytesIO(_GENERIC_CSV.encode()), "text/csv")},
        data={"account_id": str(reconcile_account.id)},
    )
    assert r.status_code == 200


async def test_bank_preview_ofx_200(
    client: AsyncClient, reconcile_account: Account
) -> None:
    r = await client.post(
        "/admin/imports/bank/preview",
        files={"file": ("test.ofx", io.BytesIO(_OFX_SNIPPET.encode()), "text/plain")},
        data={"account_id": str(reconcile_account.id)},
    )
    assert r.status_code == 200
    assert "OFX" in r.text


async def test_bank_preview_bad_csv_400(
    client: AsyncClient, reconcile_account: Account
) -> None:
    bad_csv = "col1,col2\nno_date,no_amount\n"
    r = await client.post(
        "/admin/imports/bank/preview",
        files={"file": ("bad.csv", io.BytesIO(bad_csv.encode()), "text/csv")},
        data={"account_id": str(reconcile_account.id)},
    )
    assert r.status_code == 400


async def test_bank_preview_unknown_account_400(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/imports/bank/preview",
        files={"file": ("test.csv", io.BytesIO(_GENERIC_CSV.encode()), "text/csv")},
        data={"account_id": str(uuid.uuid4())},
    )
    assert r.status_code == 400


async def test_bank_apply_valid_csv_200(
    client: AsyncClient, reconcile_account: Account
) -> None:
    r = await client.post(
        "/admin/imports/bank/apply",
        data={
            "account_id": str(reconcile_account.id),
            "raw": _GENERIC_CSV,
        },
    )
    assert r.status_code == 200
    assert "inserted" in r.text.lower() or "imported" in r.text.lower()


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------


async def test_coa_index_200(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/coa")
    assert r.status_code == 200


async def test_coa_export_200_csv(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/coa/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]


async def test_coa_export_download_flag_sets_content_disposition(
    client: AsyncClient,
) -> None:
    r = await client.get("/admin/imports/coa/export", params={"download": 1})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "coa.csv" in cd


async def test_coa_preview_valid_csv_200(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/imports/coa/preview",
        files={"file": ("coa.csv", io.BytesIO(_COA_CSV.encode()), "text/csv")},
    )
    assert r.status_code == 200


async def test_coa_preview_bad_csv_400(client: AsyncClient) -> None:
    bad = "wrong,headers\nno,match\n"
    r = await client.post(
        "/admin/imports/coa/preview",
        files={"file": ("bad.csv", io.BytesIO(bad.encode()), "text/csv")},
    )
    assert r.status_code == 400


async def test_coa_apply_redirects(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/imports/coa/apply",
        data={"raw": _COA_CSV, "archive_removed": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin/imports/coa" in r.headers["location"]


# ---------------------------------------------------------------------------
# QBO migration
# ---------------------------------------------------------------------------


async def test_qbo_index_200(client: AsyncClient) -> None:
    r = await client.get("/admin/imports/qbo")
    assert r.status_code == 200


async def test_qbo_contacts_preview_valid_200(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/imports/qbo/contacts/preview",
        files={
            "file": (
                "customers.csv",
                io.BytesIO(_QBO_CONTACTS_CSV.encode()),
                "text/csv",
            )
        },
        data={"kind": "customer"},
    )
    assert r.status_code == 200


async def test_qbo_contacts_preview_bad_csv_400(client: AsyncClient) -> None:
    bad = "no,valid,qbo,headers\na,b,c,d\n"
    r = await client.post(
        "/admin/imports/qbo/contacts/preview",
        files={"file": ("bad.csv", io.BytesIO(bad.encode()), "text/csv")},
        data={"kind": "auto"},
    )
    assert r.status_code == 400


async def test_qbo_contacts_apply_redirects(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/imports/qbo/contacts/apply",
        data={"raw": _QBO_CONTACTS_CSV, "kind": "customer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "contacts_imported" in r.headers["location"]
