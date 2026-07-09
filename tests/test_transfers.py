"""Transfer record type — service + RLS + API coverage.

Covers migration 0155 (``transfers`` table), ``services/transfers.py``, and
``/api/v1/transfers``. The Transfer is the first-class account-to-account
money-movement record (DB-rebuild handover #2): bank -> credit-card paydown,
bank -> director-loan repayment, bank/loan transfers.

Structural / RLS (Postgres only):
  * RLS ENABLE + FORCE + a ``tenant_isolation`` policy on ``transfers``.
  * Cross-tenant probe: a NOBYPASSRLS ``saebooks_app`` session scoped to tenant
    A cannot read tenant B's ``transfers`` row; with no tenant set, zero rows
    (deny by default).

Service (superuser AsyncSessionLocal — mirrors tests/test_intercompany.py):
  * Happy path: one balanced JE (Dr to / Cr from), origin=TRANSFER,
    source_type='transfer', source_id=transfer.id, transfer.journal_entry_id
    linked, no GST line.
  * Credit-card paydown + director-loan repayment sign conventions.
  * Validation: P&L account rejected, header account rejected, same account
    rejected, non-positive amount rejected, cross-company account rejected —
    nothing persists.
  * Reversal: JE -> REVERSED, transfer -> REVERSED, re-reverse raises.

API:
  * 401 without bearer; 201 create; 400 on P&L account; list + get; reverse.
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
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.tenant import Tenant
from saebooks.models.transfer import Transfer, TransferStatus
from saebooks.services import transfers as svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLE = "transfers"

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS assertions (the non-negotiable new-table checklist)
# --------------------------------------------------------------------------- #
async def test_transfers_has_force_rls() -> None:
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
    assert row is not None, "transfers absent from pg_class — migration 0155 missing"
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "RLS not ENABLE+FORCE on transfers — migration 0155 incomplete"
    )


async def test_transfers_has_tenant_isolation_policy() -> None:
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
    assert row is not None, "transfers missing tenant_isolation policy"
    assert "tenant_id" in row.qual and "current_setting" in row.qual, (
        f"transfers policy is not the standard tenant predicate: {row.qual!r}"
    )


# --------------------------------------------------------------------------- #
# Cross-tenant probe via the NOBYPASSRLS saebooks_app role
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
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + two BS accounts + one transfer row."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            from_id = uuid.uuid4()
            to_id = uuid.uuid4()
            xfer_id = uuid.uuid4()
            session.add(Tenant(id=tid, name=f"XFER-{label}-{suffix}",
                               slug=f"xfer-{label}-{suffix}"))
            await session.flush()
            session.add(Company(id=cid, tenant_id=tid,
                                name=f"XFER-{label}-{suffix}", base_currency="AUD"))
            await session.flush()
            session.add(Account(id=from_id, company_id=cid, tenant_id=tid,
                                code=f"1-10{suffix[:2]}", name="Bank",
                                account_type=AccountType.ASSET))
            session.add(Account(id=to_id, company_id=cid, tenant_id=tid,
                                code=f"2-11{suffix[:2]}", name="Credit Card",
                                account_type=AccountType.LIABILITY))
            await session.flush()
            session.add(Transfer(id=xfer_id, tenant_id=tid, company_id=cid,
                                 from_account_id=from_id, to_account_id=to_id,
                                 amount=Decimal("100.00"),
                                 transfer_date=date(2026, 6, 6),
                                 status=TransferStatus.POSTED))
            await session.flush()
            out[label] = {"tenant_id": tid, "company_id": cid, "xfer_id": xfer_id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(text("DELETE FROM transfers WHERE id = :i"),
                                  {"i": row["xfer_id"]})
            await session.execute(text("DELETE FROM accounts WHERE company_id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM companies WHERE id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM tenants WHERE id = :i"),
                                  {"i": row["tenant_id"]})
        await session.commit()


async def test_transfer_visible_to_own_tenant(
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
                text("SELECT id FROM transfers WHERE id = :i"), {"i": a["xfer_id"]}
            )
        ).all()
    assert len(visible) == 1, "tenant A cannot see its own transfer — RLS too tight"


async def test_transfer_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_xfer = seeded_two_tenants["tenant_b"]["xfer_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM transfers WHERE id = :i"), {"i": b_xfer}
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's transfer {b_xfer} — tenant_isolation broken"
    )


async def test_transfer_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (await session.execute(text("SELECT count(*) FROM transfers"))).scalar_one()
    assert rows == 0, f"expected 0 transfers with no tenant set, got {rows}"


# --------------------------------------------------------------------------- #
# Service-layer (superuser session, like test_cross_company_fk/test_intercompany)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def transfer_setup() -> AsyncIterator[dict[str, Any]]:
    """One company in DEFAULT tenant with bank (ASSET), credit-card (LIABILITY),
    directors-loan (LIABILITY), an income account (P&L, to prove rejection), a
    header account, and a SISTER company's bank account (cross-company probe).
    """
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        co = Company(name=f"XferCo-{tag}", base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        sister = Company(name=f"XferSis-{tag}", base_currency="AUD",
                         tenant_id=_DEFAULT_TENANT)
        session.add_all([co, sister])
        await session.flush()

        bank = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                       code=f"1-10{tag[:2]}", name="Bank", account_type=AccountType.ASSET)
        card = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                       code=f"2-1115{tag[:1]}", name="Credit Card",
                       account_type=AccountType.LIABILITY)
        loan = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                       code=f"2-2200{tag[:1]}", name="Directors Loan",
                       account_type=AccountType.LIABILITY)
        income = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                         code=f"4-10{tag[:2]}", name="Sales",
                         account_type=AccountType.INCOME)
        header = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                         code=f"1-00{tag[:2]}", name="Assets (header)",
                         account_type=AccountType.ASSET, is_header=True)
        sister_bank = Account(company_id=sister.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Sister Bank",
                              account_type=AccountType.ASSET)
        session.add_all([bank, card, loan, income, header, sister_bank])
        await session.commit()
        data = {
            "company_id": co.id, "sister_id": sister.id,
            "bank": bank.id, "card": card.id, "loan": loan.id,
            "income": income.id, "header": header.id,
            "sister_bank": sister_bank.id,
        }
    yield data

    async with AsyncSessionLocal() as session:
        for cid in (data["company_id"], data["sister_id"]):
            await session.execute(text(
                "DELETE FROM transfers WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM journal_lines WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM journal_entries WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM accounts WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        await session.commit()


async def test_create_and_post_transfer_happy_path(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        transfer = await svc.create_and_post_transfer(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            from_account_id=d["bank"],
            to_account_id=d["card"],
            amount=Decimal("500.00"),
            transfer_date=date(2026, 6, 6),
            description="CC paydown",
            reference="REF-1",
            posted_by="test",
        )
    assert transfer.status == TransferStatus.POSTED
    assert transfer.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        je = (await session.execute(
            select(JournalEntry).where(JournalEntry.id == transfer.journal_entry_id)
        )).scalar_one()
        assert je.status == EntryStatus.POSTED
        assert je.origin == JournalOrigin.TRANSFER
        assert je.source_type == "transfer"
        assert je.source_id == transfer.id

        lines = (await session.execute(
            select(JournalLine).where(JournalLine.entry_id == je.id)
        )).scalars().all()
        # Exactly two lines — no GST line (balance-sheet movement).
        assert len(lines) == 2, "transfer must be exactly two lines, no GST"
        tot_dr = sum(line.debit for line in lines)
        tot_cr = sum(line.credit for line in lines)
        assert tot_dr == tot_cr == Decimal("500.00")
        # Dr to_account (card / liability down), Cr from_account (bank / asset down).
        to_line = next(line for line in lines if line.account_id == d["card"])
        from_line = next(line for line in lines if line.account_id == d["bank"])
        assert to_line.debit == Decimal("500.00") and to_line.credit == Decimal("0")
        assert from_line.credit == Decimal("500.00") and from_line.debit == Decimal("0")
        assert all(line.gst_amount in (None, Decimal("0")) for line in lines)


async def test_director_loan_repayment_signs(transfer_setup: dict[str, Any]) -> None:
    """Bank -> directors-loan: Dr 2-2200 (loan down) / Cr bank (asset down)."""
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        transfer = await svc.create_and_post_transfer(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            from_account_id=d["bank"],
            to_account_id=d["loan"],
            amount=Decimal("1200.00"),
            transfer_date=date(2026, 6, 6),
            description="Director loan repayment",
            posted_by="test",
        )
    async with AsyncSessionLocal() as session:
        lines = (await session.execute(
            select(JournalLine).where(
                JournalLine.entry_id == transfer.journal_entry_id)
        )).scalars().all()
        loan_line = next(line for line in lines if line.account_id == d["loan"])
        bank_line = next(line for line in lines if line.account_id == d["bank"])
        assert loan_line.debit == Decimal("1200.00")
        assert bank_line.credit == Decimal("1200.00")


async def test_pl_account_rejected(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError, match="balance-sheet"):
            await svc.create_and_post_transfer(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                from_account_id=d["bank"],
                to_account_id=d["income"],  # P&L — must be rejected
                amount=Decimal("10.00"),
                transfer_date=date(2026, 6, 6),
                posted_by="test",
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_header_account_rejected(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError, match="header"):
            await svc.create_and_post_transfer(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                from_account_id=d["header"],  # header — must be rejected
                to_account_id=d["card"],
                amount=Decimal("10.00"),
                transfer_date=date(2026, 6, 6),
                posted_by="test",
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_same_account_rejected(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError, match="different"):
            await svc.create_and_post_transfer(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                from_account_id=d["bank"],
                to_account_id=d["bank"],
                amount=Decimal("10.00"),
                transfer_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_non_positive_amount_rejected(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError, match="positive"):
            await svc.create_and_post_transfer(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                from_account_id=d["bank"],
                to_account_id=d["card"],
                amount=Decimal("0.00"),
                transfer_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_cross_company_account_rejected(transfer_setup: dict[str, Any]) -> None:
    """A sister company's account cannot be a transfer leg (app-layer guard +
    composite FK). Validated before any JE is built."""
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError):
            await svc.create_and_post_transfer(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                from_account_id=d["bank"],
                to_account_id=d["sister_bank"],  # belongs to sister company
                amount=Decimal("10.00"),
                transfer_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_reverse_transfer(transfer_setup: dict[str, Any]) -> None:
    d = transfer_setup
    async with AsyncSessionLocal() as session:
        transfer = await svc.create_and_post_transfer(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            from_account_id=d["bank"],
            to_account_id=d["card"],
            amount=Decimal("750.00"),
            transfer_date=date(2026, 6, 6),
            posted_by="test",
        )
        original_je_id = transfer.journal_entry_id

    async with AsyncSessionLocal() as session:
        reversed_xfer = await svc.reverse_transfer(
            session, transfer.id,
            tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            reversal_date=date(2026, 6, 7), posted_by="test",
        )
    assert reversed_xfer.status == TransferStatus.REVERSED

    async with AsyncSessionLocal() as session:
        orig_je = (await session.execute(
            select(JournalEntry).where(JournalEntry.id == original_je_id)
        )).scalar_one()
        assert orig_je.status == EntryStatus.REVERSED
        # A reversal JE exists for this company (a third+ JE distinct from orig).
        all_je = (await session.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == d["company_id"])
        )).scalars().all()
        assert any(j.reversal_of_id == original_je_id for j in all_je), (
            "no reversal JE linked back to the original transfer JE"
        )

    # Idempotency: re-reversing raises.
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.TransferError, match="already reversed"):
            await svc.reverse_transfer(
                session, transfer.id,
                tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            )


async def _assert_nothing_persisted(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        n_xfer = (await session.execute(text(
            "SELECT count(*) FROM transfers WHERE company_id = :c"),
            {"c": company_id})).scalar_one()
        n_je = (await session.execute(text(
            "SELECT count(*) FROM journal_entries WHERE company_id = :c"),
            {"c": company_id})).scalar_one()
    assert n_xfer == 0 and n_je == 0, (
        f"rejected transfer left state: transfers={n_xfer} je={n_je}"
    )


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def api_company() -> AsyncIterator[dict[str, Any]]:
    """A company in DEFAULT tenant with bank + card + income accounts, for the
    HTTP surface. Passed via X-Company-Id so the test is self-contained."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        co = Company(name=f"XferApi-{tag}", base_currency="AUD",
                     tenant_id=_DEFAULT_TENANT)
        session.add(co)
        await session.flush()
        bank = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                       code=f"1-10{tag[:2]}", name="Bank",
                       account_type=AccountType.ASSET)
        card = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                       code=f"2-1115{tag[:1]}", name="Credit Card",
                       account_type=AccountType.LIABILITY)
        income = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                         code=f"4-10{tag[:2]}", name="Sales",
                         account_type=AccountType.INCOME)
        session.add_all([bank, card, income])
        await session.commit()
        data = {"company_id": co.id, "bank": bank.id, "card": card.id,
                "income": income.id}
    yield data
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM transfers WHERE company_id = :c"), {"c": data["company_id"]})
        await session.execute(text(
            "DELETE FROM journal_lines WHERE company_id = :c"), {"c": data["company_id"]})
        await session.execute(text(
            "DELETE FROM journal_entries WHERE company_id = :c"), {"c": data["company_id"]})
        await session.execute(text(
            "DELETE FROM accounts WHERE company_id = :c"), {"c": data["company_id"]})
        await session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": data["company_id"]})
        await session.commit()


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
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
    r = await unauth_client.get("/api/v1/transfers")
    assert r.status_code == 401


async def test_api_create_list_get_reverse(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    cid = str(api_company["company_id"])
    hdr = {"X-Company-Id": cid}
    body = {
        "from_account_id": str(api_company["bank"]),
        "to_account_id": str(api_company["card"]),
        "amount": "320.00",
        "transfer_date": "2026-06-06",
        "description": "CC paydown via API",
        "reference": "INV-CC-1",
    }
    r = await api_client.post("/api/v1/transfers", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["amount"] == "320.00"
    assert out["status"] == "POSTED"
    assert out["journal_entry_id"]
    xfer_id = out["id"]

    # List
    r = await api_client.get("/api/v1/transfers", headers=hdr)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any(it["id"] == xfer_id for it in items)

    # Get
    r = await api_client.get(f"/api/v1/transfers/{xfer_id}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["id"] == xfer_id

    # Reverse
    r = await api_client.post(
        f"/api/v1/transfers/{xfer_id}/reverse", json={}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERSED"

    # Re-reverse -> 409
    r = await api_client.post(
        f"/api/v1/transfers/{xfer_id}/reverse", json={}, headers=hdr
    )
    assert r.status_code == 409, r.text


async def test_api_create_pl_account_400(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    hdr = {"X-Company-Id": str(api_company["company_id"])}
    body = {
        "from_account_id": str(api_company["bank"]),
        "to_account_id": str(api_company["income"]),  # P&L
        "amount": "50.00",
        "transfer_date": "2026-06-06",
    }
    r = await api_client.post("/api/v1/transfers", json=body, headers=hdr)
    assert r.status_code == 400, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "transfer_invalid"


async def test_api_get_unknown_404(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    hdr = {"X-Company-Id": str(api_company["company_id"])}
    r = await api_client.get(f"/api/v1/transfers/{uuid.uuid4()}", headers=hdr)
    assert r.status_code == 404, r.text
