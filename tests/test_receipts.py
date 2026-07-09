"""Generic money-in receipt — service + posting + RLS + API.

Covers migration 0157 (``receipts`` + ``receipt_lines``),
``services/receipts.py`` and ``/api/v1/receipts``.

A receipt is the engine's generic money-in record for refunds / cashbacks /
rebates / ATO GST refund / insurance recovery not tied to a bill:
Dr bank/asset, Cr income|expense, Cr GST (Collected for income lines, Paid for
expense lines).

Structural / RLS (Postgres only): ENABLE + FORCE + tenant_isolation on
``receipts``; cross-tenant probe via NOBYPASSRLS saebooks_app.

Service: income-line receipt (interest received — Cr income, Cr GST Collected);
expense-line receipt (materials cashback — Cr expense, Cr GST Paid); GST-free
ATO GST refund (Dr bank / Cr ... no, GST refund credits the GST clearing/asset);
validation (income/expense only; ASSET destination only).

API: 401 without bearer; create/post/get.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import (
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.receipt import Receipt, ReceiptStatus
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.services import receipts as svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLE = "receipts"
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS
# --------------------------------------------------------------------------- #
async def test_receipts_has_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :n"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, "receipts absent — migration 0157 missing"
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


async def test_receipts_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT qual FROM pg_policies "
                    "WHERE tablename = :n AND policyname = 'tenant_isolation'"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None
    assert "tenant_id" in row.qual and "current_setting" in row.qual


# --------------------------------------------------------------------------- #
# Cross-tenant probe
# --------------------------------------------------------------------------- #
def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


async def _ensure_app_role_login() -> bool:
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return False
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            bank_id = uuid.uuid4()
            rcpt_id = uuid.uuid4()
            session.add(Tenant(id=tid, name=f"RCPT-{label}-{suffix}",
                               slug=f"rcpt-{label}-{suffix}"))
            await session.flush()
            session.add(Company(id=cid, tenant_id=tid,
                                name=f"RCPT-{label}-{suffix}", base_currency="AUD"))
            await session.flush()
            session.add(Account(id=bank_id, company_id=cid, tenant_id=tid,
                                code=f"1-10{suffix[:2]}", name="Bank",
                                account_type=AccountType.ASSET))
            await session.flush()
            session.add(Receipt(
                id=rcpt_id, tenant_id=tid, company_id=cid, bank_account_id=bank_id,
                receipt_date=date(2026, 6, 6), status=ReceiptStatus.DRAFT,
                subtotal=Decimal("0"), tax_total=Decimal("0"), total=Decimal("0")))
            await session.flush()
            out[label] = {"tenant_id": tid, "company_id": cid, "rcpt_id": rcpt_id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(text("DELETE FROM receipts WHERE id = :i"),
                                  {"i": row["rcpt_id"]})
            await session.execute(text("DELETE FROM accounts WHERE company_id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM companies WHERE id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM tenants WHERE id = :i"),
                                  {"i": row["tenant_id"]})
        await session.commit()


async def test_receipt_visible_to_own_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_two_tenants["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM receipts WHERE id = :i"), {"i": a["rcpt_id"]}
            )
        ).all()
    assert len(visible) == 1


async def test_receipt_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_rcpt = seeded_two_tenants["tenant_b"]["rcpt_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM receipts WHERE id = :i"), {"i": b_rcpt}
            )
        ).all()
    assert len(visible) == 0, "tenant A leaked tenant B's receipt"


async def test_receipt_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (await session.execute(text("SELECT count(*) FROM receipts"))).scalar_one()
    assert rows == 0


# --------------------------------------------------------------------------- #
# Service — seed company
# --------------------------------------------------------------------------- #
async def _seed_ctx() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None

        async def _acct(code: str) -> Account:
            return (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id, Account.code == code
                    )
                )
            ).scalar_one()

        bank = await _acct("1-1110")
        income = await _acct("4-6000")
        expense = await _acct("6-1000")
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()
        return {
            "company_id": company.id,
            "tenant_id": company.tenant_id,
            "bank_id": bank.id,
            "income_id": income.id,
            "expense_id": expense.id,
            "gst_id": gst.id,
        }


def _je_by_code(lines: list[JournalLine], accts: dict[uuid.UUID, str]) -> dict:
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = accts[ln.account_id]
        d_c = out.get(code, (Decimal("0"), Decimal("0")))
        out[code] = (d_c[0] + ln.debit, d_c[1] + ln.credit)
    return out


async def test_income_receipt_dr_bank_cr_income_cr_gst_collected() -> None:
    """Interest/insurance recovery — Dr bank / Cr income / Cr GST Collected."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        rcpt = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            bank_account_id=d["bank_id"], receipt_date=date(2026, 6, 6),
            reason="RCV-RACQ insurance recovery",
            lines=[{
                "description": "Insurance recovery",
                "account_id": str(d["income_id"]),
                "tax_code_id": str(d["gst_id"]),
                "amount": "100.00",
            }],
        )
    assert rcpt.subtotal == Decimal("100.00")
    assert rcpt.tax_total == Decimal("10.00")
    assert rcpt.total == Decimal("110.00")

    async with AsyncSessionLocal() as session:
        posted = await svc.api_post(
            session, rcpt.id, "test", rcpt.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
    assert posted.status == ReceiptStatus.POSTED
    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        assert entry is not None
        assert entry.origin == JournalOrigin.RECEIPT
        assert entry.source_type == "receipt"
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        accts = {
            a.id: a.code
            for a in (
                await session.execute(
                    select(Account).where(
                        Account.id.in_([ln.account_id for ln in lines])
                    )
                )
            ).scalars().all()
        }
        by_code = _je_by_code(lines, accts)
        assert sum(d_c[0] for d_c in by_code.values()) == Decimal("110.00")
        assert by_code["1-1110"] == (Decimal("110.00"), Decimal("0"))  # Dr bank
        assert by_code["4-6000"] == (Decimal("0"), Decimal("100.00"))  # Cr income
        assert by_code["2-1310"] == (Decimal("0"), Decimal("10.00"))   # Cr GST Coll


async def test_expense_receipt_dr_bank_cr_expense_cr_gst_paid() -> None:
    """Materials cashback against an expense — Dr bank / Cr expense / Cr GST Paid."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        rcpt = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            bank_account_id=d["bank_id"], receipt_date=date(2026, 6, 6),
            reason="RDX cashback",
            lines=[{
                "description": "Supplier cashback",
                "account_id": str(d["expense_id"]),
                "tax_code_id": str(d["gst_id"]),
                "amount": "200.00",
            }],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.api_post(
            session, rcpt.id, "test", rcpt.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        accts = {
            a.id: a.code
            for a in (
                await session.execute(
                    select(Account).where(
                        Account.id.in_([ln.account_id for ln in lines])
                    )
                )
            ).scalars().all()
        }
        by_code = _je_by_code(lines, accts)
        assert by_code["1-1110"] == (Decimal("220.00"), Decimal("0"))  # Dr bank
        assert by_code["6-1000"] == (Decimal("0"), Decimal("200.00"))  # Cr expense
        # Expense line GST credits GST Paid (reverses input credit).
        assert by_code["2-1330"] == (Decimal("0"), Decimal("20.00"))


async def test_gst_free_ato_refund_dr_bank_cr_account() -> None:
    """ATO GST refund example: no tax code → Dr bank / Cr account, no GST line."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        # Credit the GST Paid (asset) account directly — receiving the refund of
        # input credits. Modelled as a no-GST receipt line on an asset is not
        # allowed (line must be income/expense); use an income "ATO refund" line
        # with no tax code to demonstrate the GST-free path.
        rcpt = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            bank_account_id=d["bank_id"], receipt_date=date(2026, 6, 6),
            reason="ATO GST refund received",
            lines=[{
                "description": "ATO GST refund",
                "account_id": str(d["income_id"]),
                "tax_code_id": None,
                "amount": "500.00",
            }],
        )
    assert rcpt.tax_total == Decimal("0")
    assert rcpt.total == Decimal("500.00")
    async with AsyncSessionLocal() as session:
        posted = await svc.api_post(
            session, rcpt.id, "test", rcpt.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        # Exactly two lines — no GST line for a GST-free receipt.
        assert len(lines) == 2
        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits == Decimal("500.00")


async def test_liability_destination_rejected() -> None:
    """Destination must be an ASSET (bank) account — a liability is rejected."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        ap = (
            await session.execute(
                select(Account).where(
                    Account.company_id == d["company_id"], Account.code == "2-1200"
                )
            )
        ).scalar_one()
        rcpt = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            bank_account_id=ap.id, receipt_date=date(2026, 6, 6),
            lines=[{
                "description": "bad",
                "account_id": str(d["income_id"]),
                "amount": "10.00",
            }],
        )
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReceiptError, match="ASSET"):
            await svc.api_post(
                session, rcpt.id, "test", rcpt.version,
                tenant_id=d["tenant_id"], company_id=d["company_id"],
            )


async def test_balance_sheet_line_account_rejected() -> None:
    """A line account must be income/expense — a balance-sheet account rejects."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        rcpt = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            bank_account_id=d["bank_id"], receipt_date=date(2026, 6, 6),
            lines=[{
                "description": "bad",
                "account_id": str(d["bank_id"]),  # ASSET line — not allowed
                "amount": "10.00",
            }],
        )
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReceiptError, match="income- or expense-type"):
            await svc.api_post(
                session, rcpt.id, "test", rcpt.version,
                tenant_id=d["tenant_id"], company_id=d["company_id"],
            )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def unauth_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_api_unauth_rejected(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/receipts")
    assert r.status_code == 401


async def test_api_create_post_get(api_client: AsyncClient) -> None:
    d = await _seed_ctx()
    hdr = {"X-Company-Id": str(d["company_id"])}
    body = {
        "bank_account_id": str(d["bank_id"]),
        "receipt_date": "2026-06-06",
        "reference": "RCPT-API-1",
        "lines": [
            {
                "description": "Refund",
                "account_id": str(d["expense_id"]),
                "tax_code_id": str(d["gst_id"]),
                "amount": "150.00",
            }
        ],
    }
    r = await api_client.post("/api/v1/receipts", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["total"] == "165.00"
    rid = out["id"]

    r = await api_client.post(
        f"/api/v1/receipts/{rid}/post", headers={**hdr, "If-Match": str(out["version"])}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "POSTED"

    r = await api_client.get(f"/api/v1/receipts/{rid}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["status"] == "POSTED"
