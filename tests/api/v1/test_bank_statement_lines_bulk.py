"""Contract + RLS tests for ``POST /api/v1/bank_statement_lines/bulk``.

The bulk fact API (capture step 1, gitea #32) is the single write surface
the CSV/OFX importer and the bank-feeds ingest both funnel through. These
tests pin the two dedup strategies exactly as the in-process writers behave
and prove the created rows obey RLS:

* FINGERPRINT (no ``bank_feed_account_id``): re-posting the same file yields
  ``created=0``; three genuinely-distinct rows that share a base fingerprint
  all survive (the $50-loss regression).
* EXTERNAL_ID (``bank_feed_account_id`` set): ON CONFLICT DO NOTHING on
  ``(bank_feed_account_id, external_id)``; a re-sync of the same txns is a
  no-op. Missing ``external_id`` in feed mode is a 422.
* Cross-tenant: rows created via the endpoint under the default tenant are
  invisible to a NOBYPASSRLS ``saebooks_app`` session bound to another
  tenant (RLS on ``bank_statement_lines`` fires on the bulk-written rows).

The app-role engine pattern mirrors
``tests/services/bank_feeds/test_rls_bank_feed_accounts.py`` /
``tests/test_rls_preaccounting_schema.py``.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token, resolve_tenant_id
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.bank_statement import BankStatementLine

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def bank_account_id(api_client: AsyncClient) -> str:
    payload = {
        "code": f"BLK-{uuid.uuid4().hex[:8].upper()}",
        "name": "Bulk import test bank",
        "bsb": "063-002",
        "bank_account_number": "11223344",
        "bank_account_title": "Bulk Test",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _line(**over: object) -> dict:
    base = {
        "txn_date": "2026-02-02",
        "amount": "25.00",
        "description": "iiNet Refund",
    }
    base.update(over)
    return base


async def _count_lines(account_id: str) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count(BankStatementLine.id)).where(
                        BankStatementLine.account_id == uuid.UUID(account_id)
                    )
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# FINGERPRINT strategy (CSV / OFX)
# ---------------------------------------------------------------------------


async def test_bulk_reimport_same_file_zero_new(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Re-posting the identical batch adds zero rows (idempotent)."""
    body = {
        "account_id": bank_account_id,
        "lines": [_line(amount="10.00", description="A"), _line(amount="-5.00", description="B")],
    }
    r1 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r1.status_code == 201, r1.text
    j1 = r1.json()
    assert j1["created"] == 2
    assert j1["deduped"] == 0
    assert len(j1["ids"]) == 2

    r2 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r2.status_code == 201, r2.text
    j2 = r2.json()
    assert j2["created"] == 0
    assert j2["deduped"] == 2
    assert j2["ids"] == []
    assert await _count_lines(bank_account_id) == 2


async def test_bulk_intra_batch_duplicate_amounts_survive(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Three identical lines (no reference) must all persist — the $50 loss."""
    body = {
        "account_id": bank_account_id,
        "lines": [_line(), _line(), _line()],
    }
    r = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["created"] == 3
    assert j["deduped"] == 0
    assert await _count_lines(bank_account_id) == 3

    # And re-posting the same three still nets zero (occurrence sequence replays).
    r2 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r2.json()["created"] == 0
    assert r2.json()["deduped"] == 3
    assert await _count_lines(bank_account_id) == 3


async def test_bulk_empty_lines_is_noop(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    r = await api_client.post(
        "/api/v1/bank_statement_lines/bulk",
        json={"account_id": bank_account_id, "lines": []},
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"created": 0, "deduped": 0, "ids": []}


# ---------------------------------------------------------------------------
# EXTERNAL_ID strategy (bank feed provenance)
# ---------------------------------------------------------------------------


@pytest.fixture
async def feed_account(bank_account_id: str) -> AsyncIterator[dict[str, str]]:
    """A BankFeedClient + BankFeedAccount mapped to the test bank account.

    Neither model carries ``tenant_id`` — their RLS is indirect via the
    ``companies`` FK (Class B), so only ``company_id`` is set here.
    """
    async with AsyncSessionLocal() as session:
        acct = await session.get(Account, uuid.UUID(bank_account_id))
        assert acct is not None
        client = BankFeedClient(
            company_id=acct.company_id,
            sds_client_id=f"cli-{uuid.uuid4().hex[:10]}",
        )
        session.add(client)
        await session.flush()
        feed = BankFeedAccount(
            company_id=acct.company_id,
            bank_feed_client_id=client.id,
            ledger_account_id=acct.id,
            sds_account_id=f"acct-{uuid.uuid4().hex[:10]}",
            sds_institution_id="INST1",
        )
        session.add(feed)
        await session.commit()
        feed_id = str(feed.id)
        client_id = client.id
    yield {"feed_account_id": feed_id, "account_id": bank_account_id}
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(BankStatementLine).where(
                BankStatementLine.bank_feed_account_id == uuid.UUID(feed_id)
            )
        )
        await session.execute(
            delete(BankFeedAccount).where(BankFeedAccount.id == uuid.UUID(feed_id))
        )
        await session.execute(
            delete(BankFeedClient).where(BankFeedClient.id == client_id)
        )
        await session.commit()


async def test_bulk_feed_provenance_on_conflict(
    api_client: AsyncClient, feed_account: dict[str, str]
) -> None:
    """Feed mode dedupes on (bank_feed_account_id, external_id) via ON CONFLICT."""
    body = {
        "account_id": feed_account["account_id"],
        "bank_feed_account_id": feed_account["feed_account_id"],
        "lines": [
            {"txn_date": "2026-04-10", "amount": "125.50", "description": "Coffee", "external_id": "txn-A"},
            {"txn_date": "2026-04-11", "amount": "-42.00", "description": "Parking", "external_id": "txn-B"},
        ],
    }
    r1 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r1.status_code == 201, r1.text
    assert r1.json()["created"] == 2

    # Same payload again → zero new (ON CONFLICT DO NOTHING).
    r2 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r2.json()["created"] == 0
    assert r2.json()["deduped"] == 2

    # One new txn plus the two existing → exactly 1 insert.
    body["lines"].append(
        {"txn_date": "2026-04-12", "amount": "7.77", "description": "New", "external_id": "txn-C"}
    )
    r3 = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r3.json()["created"] == 1
    assert r3.json()["deduped"] == 2


async def test_bulk_feed_mode_requires_external_id(
    api_client: AsyncClient, feed_account: dict[str, str]
) -> None:
    """A feed-provenance request with a line missing external_id is a 422."""
    body = {
        "account_id": feed_account["account_id"],
        "bank_feed_account_id": feed_account["feed_account_id"],
        "lines": [{"txn_date": "2026-04-10", "amount": "1.00", "description": "no id"}],
    }
    r = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r.status_code == 422, r.text
    assert "external_id" in r.text


# ---------------------------------------------------------------------------
# Auth + cross-tenant RLS
# ---------------------------------------------------------------------------


async def test_bulk_requires_bearer() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/api/v1/bank_statement_lines/bulk",
            json={"account_id": str(uuid.uuid4()), "lines": []},
        )
    assert r.status_code == 401


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


@pytest_asyncio.fixture
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


async def test_bulk_created_rows_isolated_by_rls(
    api_client: AsyncClient, bank_account_id: str, app_engine: Any
) -> None:
    """Rows created through the endpoint under the default tenant are visible
    to that tenant under RLS and invisible to any other tenant."""
    body = {
        "account_id": bank_account_id,
        "lines": [_line(amount="99.00", description="RLS probe")],
    }
    r = await api_client.post("/api/v1/bank_statement_lines/bulk", json=body)
    assert r.status_code == 201, r.text
    line_id = r.json()["ids"][0]

    default_tenant = str(resolve_tenant_id(None))
    other_tenant = str(uuid.uuid4())

    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )

    # Bound to the owning (default) tenant → visible.
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": default_tenant},
        )
        seen = (
            await session.execute(
                text("SELECT id FROM bank_statement_lines WHERE id = :lid"),
                {"lid": line_id},
            )
        ).all()
    assert len(seen) == 1, "owning tenant could not see its own bulk-created line"

    # Bound to a foreign tenant → invisible (RLS fires on the written row).
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": other_tenant},
        )
        leaked = (
            await session.execute(
                text("SELECT id FROM bank_statement_lines WHERE id = :lid"),
                {"lid": line_id},
            )
        ).all()
    assert leaked == [], "CROSS-TENANT LEAK: bulk-created line visible to another tenant"
