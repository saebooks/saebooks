"""Tests for ``GET /api/v1/accounts/{id}/gl-movement`` (capture fact API).

The endpoint exposes exactly the aggregation the bank-feeds reconcile sweep
needs: ``SUM(debit - credit)`` over journal entries in status POSTED or
REVERSED on one account, optionally bounded by entry date. The endpoint and
``services.bank_feeds.reconcile.sweep`` call the same ``gl_movement``
function, so this fixture also pins the sweep's variance numerator.

Fixture ledger (all on the same asset account):
    POSTED  +100.00  (debit 100)
    POSTED   -30.00  (credit 30)
    REVERSED +50.00  (debit 50)   ← REVERSED still counts (void cancels reversal)
    DRAFT   +999.00  (debit 999)  ← excluded
    => all-time movement = 100 - 30 + 50 = 120.00
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client(seeded_company) -> AsyncClient:
    """Bearer client pinned to the throwaway seeded company via X-Company-Id."""
    company_id, _tenant_id, _accounts = seeded_company
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    ) as ac:
        yield ac


async def _entry(
    session, *, company_id, tenant_id, target_id, contra_id, ref, status,
    target_debit, target_credit, entry_date,
):
    """Create a balanced 2-line entry: the ``target`` account gets the
    intended movement, the ``contra`` account balances it (a DB trigger
    requires POSTED entries to have >=2 lines and balance)."""
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=entry_date,
        status=status,
    )
    session.add(entry)
    await session.flush()
    session.add_all(
        [
            JournalLine(
                entry_id=entry.id, company_id=company_id, line_no=1,
                account_id=target_id,
                debit=Decimal(target_debit), credit=Decimal(target_credit),
            ),
            # Contra mirrors the target so debits == credits.
            JournalLine(
                entry_id=entry.id, company_id=company_id, line_no=2,
                account_id=contra_id,
                debit=Decimal(target_credit), credit=Decimal(target_debit),
            ),
        ]
    )
    await session.flush()
    return entry.id


@pytest.fixture
async def gl_fixture(seeded_company):
    company_id, tenant_id, accounts = seeded_company
    asset_id, contra_id = accounts[0], accounts[1]
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        await _entry(session, company_id=company_id, tenant_id=tenant_id,
                     target_id=asset_id, contra_id=contra_id,
                     ref=f"GLM-{tag}-1", status=EntryStatus.POSTED,
                     target_debit="100.00", target_credit="0",
                     entry_date=date(2026, 2, 1))
        await _entry(session, company_id=company_id, tenant_id=tenant_id,
                     target_id=asset_id, contra_id=contra_id,
                     ref=f"GLM-{tag}-2", status=EntryStatus.POSTED,
                     target_debit="0", target_credit="30.00",
                     entry_date=date(2026, 3, 1))
        await _entry(session, company_id=company_id, tenant_id=tenant_id,
                     target_id=asset_id, contra_id=contra_id,
                     ref=f"GLM-{tag}-3", status=EntryStatus.REVERSED,
                     target_debit="50.00", target_credit="0",
                     entry_date=date(2026, 4, 1))
        await _entry(session, company_id=company_id, tenant_id=tenant_id,
                     target_id=asset_id, contra_id=contra_id,
                     ref=f"GLM-{tag}-4", status=EntryStatus.DRAFT,
                     target_debit="999.00", target_credit="0",
                     entry_date=date(2026, 5, 1))
        await session.commit()
    yield {"account_id": str(asset_id), "company_id": str(company_id)}
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(JournalEntry).where(JournalEntry.company_id == company_id)
        )
        await session.commit()


async def test_gl_movement_matches_hand_computed_total(
    api_client: AsyncClient, gl_fixture: dict
) -> None:
    r = await api_client.get(
        f"/api/v1/accounts/{gl_fixture['account_id']}/gl-movement"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 100 - 30 + 50 = 120 ; DRAFT 999 excluded, REVERSED 50 included.
    assert Decimal(body["movement"]) == Decimal("120")
    assert body["account_id"] == gl_fixture["account_id"]
    assert body["date_from"] is None
    assert body["date_to"] is None


async def test_gl_movement_date_bounds(
    api_client: AsyncClient, gl_fixture: dict
) -> None:
    # Only Feb + Mar (100 posted, -30 posted) → 70; the REVERSED Apr entry
    # is outside the window.
    r = await api_client.get(
        f"/api/v1/accounts/{gl_fixture['account_id']}/gl-movement",
        params={"date_from": "2026-02-01", "date_to": "2026-03-31"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["movement"]) == Decimal("70")


async def test_gl_movement_includes_reversed_only_window(
    api_client: AsyncClient, gl_fixture: dict
) -> None:
    # April-only window isolates the REVERSED entry → proves REVERSED counts.
    r = await api_client.get(
        f"/api/v1/accounts/{gl_fixture['account_id']}/gl-movement",
        params={"date_from": "2026-04-01", "date_to": "2026-04-30"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["movement"]) == Decimal("50")


async def test_gl_movement_unknown_account_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/accounts/{uuid.uuid4()}/gl-movement")
    assert r.status_code == 404


async def test_gl_movement_bad_date_400(
    api_client: AsyncClient, gl_fixture: dict
) -> None:
    r = await api_client.get(
        f"/api/v1/accounts/{gl_fixture['account_id']}/gl-movement",
        params={"date_from": "not-a-date"},
    )
    assert r.status_code == 400
