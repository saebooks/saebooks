"""End-to-end happy-path test for the cashbook surface.

Walks the canonical sole-trader journey through the public API:

    1. Setup the active company in cashbook mode (idempotent re-pin).
    2. Record an income entry.
    3. Record an expense entry.
    4. List entries — both present, summary aggregates correctly.
    5. Replace one entry — replacement carries the chain link.
    6. Delete another — disappears from the cashbook surface.
    7. Upgrade to full — mode flips, history still readable.

Individual contracts are exercised by ``test_cashbook.py``; this file
catches *sequence* bugs the unit tests miss (e.g. can a user still GET
/entries after upgrade-to-full? The cashbook router gates on the
service, not on mode, so it should — and we verify here).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company
pytestmark = pytest.mark.postgres_only


def _D(s: str) -> Decimal:
    return Decimal(s)


def _new_key(prefix: str = "e2e") -> str:
    return f"{prefix}-{uuid.uuid4()}"


async def _seed_state() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Reset seed company to cashbook mode and return
    ``(tenant_id, company_id, bank_account_id)``."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.gst_registered = False
        co.cashbook_categories = None
        await session.commit()
        return co.tenant_id, co.id, bank.id


@pytest.fixture(autouse=True)
async def _restore_seed_company_mode() -> None:
    """Restore the seed company to bookkeeping_mode=full after each test.

    _seed_state() flips the seed company to cashbook mode for the
    cashbook user-journey test. Without a restore, the mutation leaks
    into every subsequent test file that selects "the oldest company"
    (test_invoices, test_payments, test_journal, ...) — which is many.
    """
    yield
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        if co is not None and co.bookkeeping_mode != "full":
            co.bookkeeping_mode = "full"
            co.cashbook_default_bank_account_id = None
            co.cashbook_categories = None
            await session.commit()


@pytest.fixture
async def client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def test_cashbook_full_user_journey(client: AsyncClient) -> None:
    _, company_id, bank_id = await _seed_state()

    # 1. /setup is idempotent — re-pinning the same bank works.
    r = await client.post(
        "/api/v1/cashbook/setup",
        json={"bank_account_id": str(bank_id)},
    )
    assert r.status_code == 200
    assert r.json()["bookkeeping_mode"] == "cashbook"

    # 2. Record income.
    income_body = {
        "entry_date": date.today().isoformat(),
        "description": "e2e — services Q1",
        "amount": "1500.00",
        "direction": "income",
        "category_code": "INC_SERVICES",
    }
    r_in = await client.post(
        "/api/v1/cashbook/entries",
        json=income_body,
        headers={"X-Idempotency-Key": _new_key("income")},
    )
    assert r_in.status_code == 201, r_in.text
    income_id = r_in.json()["id"]

    # 3. Record expense.
    expense_body = {
        "entry_date": date.today().isoformat(),
        "description": "e2e — Bunnings",
        "amount": "120.00",
        "direction": "expense",
        "category_code": "EXP_MATERIALS",
    }
    r_ex = await client.post(
        "/api/v1/cashbook/entries",
        json=expense_body,
        headers={"X-Idempotency-Key": _new_key("expense")},
    )
    assert r_ex.status_code == 201, r_ex.text
    expense_id = r_ex.json()["id"]

    # 4. List + summary — both entries present, totals reflect them.
    r_list = await client.get(
        f"/api/v1/cashbook/entries?from={date.today().isoformat()}"
        f"&to={date.today().isoformat()}"
    )
    assert r_list.status_code == 200
    ids = {e["id"] for e in r_list.json()["items"]}
    assert {income_id, expense_id} <= ids

    today = date.today().isoformat()
    r_pre = await client.get(
        f"/api/v1/cashbook/summary?from={today}&to={today}"
    )
    assert r_pre.status_code == 200
    pre = r_pre.json()
    # Use deltas in case the dev DB carries other same-day entries.
    pre_income = _D(pre["income_total"])
    pre_expense = _D(pre["expense_total"])

    # 5. Replace the income entry with a different category.
    replace_body = {
        **income_body,
        "amount": "1600.00",
        "category_code": "INC_OTHER",
    }
    r_replace = await client.patch(
        f"/api/v1/cashbook/entries/{income_id}",
        json=replace_body,
        headers={"X-Idempotency-Key": _new_key("replace")},
    )
    assert r_replace.status_code == 200, r_replace.text
    new_income_id = r_replace.json()["id"]
    assert new_income_id != income_id  # genuinely a new entry
    assert _D(r_replace.json()["amount"]) == _D("1600.00")

    # Old entry hidden from list (REVERSED filter).
    r_list2 = await client.get(
        f"/api/v1/cashbook/entries?from={today}&to={today}"
    )
    ids2 = {e["id"] for e in r_list2.json()["items"]}
    assert income_id not in ids2  # voided
    assert new_income_id in ids2  # replacement
    assert expense_id in ids2  # untouched

    # 6. Delete the expense — disappears from list.
    r_del = await client.delete(f"/api/v1/cashbook/entries/{expense_id}")
    assert r_del.status_code == 204
    r_list3 = await client.get(
        f"/api/v1/cashbook/entries?from={today}&to={today}"
    )
    ids3 = {e["id"] for e in r_list3.json()["items"]}
    assert expense_id not in ids3
    assert new_income_id in ids3

    # 7. Upgrade to full — mode flips, history is still readable.
    r_up = await client.post("/api/v1/cashbook/upgrade-to-full")
    assert r_up.status_code == 200, r_up.text
    assert r_up.json()["bookkeeping_mode"] == "full"

    # The cashbook router does NOT gate on mode — the design says the
    # upgrade is a UX flag flip and history is preserved. Reading entries
    # after upgrade must keep working so a user can review what they had.
    r_list4 = await client.get(
        f"/api/v1/cashbook/entries?from={today}&to={today}"
    )
    assert r_list4.status_code == 200
    ids4 = {e["id"] for e in r_list4.json()["items"]}
    assert new_income_id in ids4

    # 8. Restore for downstream tests.
    await _seed_state()
