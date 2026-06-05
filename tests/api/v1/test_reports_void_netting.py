"""Regression: voiding/reversing a journal entry must NET TO ZERO in reports.

Live bug (verified 2026-06-06): ``journal.reverse()`` posts a mirror reversal
JE *and* flips the original to ``status=REVERSED``. The report queries summed
only ``status == POSTED`` rows, so they DROPPED the REVERSED original but still
COUNTED the POSTED reversal — the reversal subtracted a SECOND time.
Symptom: voiding a $1,580 invoice moved Trade Debtors 5790.40 → 4210.40 and
understated income via GET /api/v1/reports/trial_balance.

Fix (option (a)): include ``REVERSED`` in the reportable-status set so the
original + its POSTED reversal cancel exactly. This file pins:

* test_void_nets_to_zero_same_period — the live symptom: a same-date
  create→post→reverse leaves the (dedicated-account) trial balance unchanged.
* test_reversal_in_later_period_keeps_original_live — the period-boundary
  caveat that makes option (a) correct and (b) wrong: a reversal dated in a
  LATER period must NOT retroactively erase the original from a trial balance
  taken BETWEEN the two dates.

Isolation: each test creates its OWN dedicated GL accounts with unique codes,
so a shared-DB full-suite run cannot bleed other tests' postings into the
asserted balances. We compare only the two accounts this test owns.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app
from saebooks.models.account import AccountType

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def _make_account(client: AsyncClient, account_type: AccountType) -> str:
    """Create a dedicated, uniquely-coded account; return its id.

    Unique codes guarantee no other test in a shared-DB run posts to these
    accounts, so balance assertions on them are deterministic.
    """
    # Account codes must be {prefix}-{digits} (optional letter suffix). Use a
    # high, unlikely-to-collide numeric prefix + a unique numeric suffix so
    # other tests never touch these accounts in a shared-DB run.
    suffix = uuid.uuid4().int % 1_000_000_000
    r = await client.post(
        "/api/v1/accounts",
        json={
            "code": f"99-{suffix:09d}",
            "name": f"void-netting {account_type.value} {suffix}",
            "account_type": account_type.value,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _net_balances(
    client: AsyncClient, as_of: str, account_ids: set[str]
) -> dict:
    """{account_id: net_balance (debit - credit)} restricted to account_ids.

    Net balance (not raw debit/credit totals) is the right invariant for the
    netting assertion: an account that has never been posted to is absent from
    the trial balance, while one whose original + reversal cancel reports
    debit_total == credit_total → net 0. Both must read as net 0, so we key on
    the net and default a missing account to 0.0."""
    r = await client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": as_of, "include_zero_balance": "true"},
    )
    assert r.status_code == 200, r.text
    seen = {
        a["account_id"]: round(a["debit_total"] - a["credit_total"], 2)
        for a in r.json()["accounts"]
        if a["account_id"] in account_ids
    }
    return {acc: seen.get(acc, 0.0) for acc in account_ids}


async def _create_and_post_je(
    client: AsyncClient, entry_date: str, lines: list[dict]
) -> dict:
    r = await client.post(
        "/api/v1/journal_entries",
        json={"entry_date": entry_date, "narration": "void-netting test", "lines": lines},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.patch(
        f"/api/v1/journal_entries/{body['id']}",
        json={"status": "POSTED"},
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _reverse_je(
    client: AsyncClient, je: dict, reversal_date: str | None = None
) -> dict:
    payload: dict = {}
    if reversal_date is not None:
        payload["reversal_date"] = reversal_date
    r = await client.post(
        f"/api/v1/journal_entries/{je['id']}/reverse",
        json=payload,
        headers={"If-Match": str(je["version"])},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_void_nets_to_zero_same_period(api_client: AsyncClient) -> None:
    """Create→post→reverse (same date) ⇒ owned-account balances unchanged.

    The live symptom. A 1580.00 entry then its reversal must net to zero on
    the accounts it touches — NOT subtract twice.
    """
    asset_id = await _make_account(api_client, AccountType.ASSET)
    income_id = await _make_account(api_client, AccountType.INCOME)
    owned = {asset_id, income_id}
    as_of = "2028-01-31"

    before = await _net_balances(api_client, as_of, owned)
    assert before == {asset_id: 0.0, income_id: 0.0}, before

    je = await _create_and_post_je(
        api_client,
        "2028-01-10",
        lines=[
            {"account_id": asset_id, "debit": "1580.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "1580.00"},
        ],
    )

    posted = await _net_balances(api_client, as_of, owned)
    assert posted == {asset_id: 1580.0, income_id: -1580.0}, (
        f"posted JE should move the owned-account net balances: {posted}"
    )

    await _reverse_je(api_client, je)  # default → same entry_date as original

    after = await _net_balances(api_client, as_of, owned)
    assert after == before, (
        "void must net to ZERO on the owned accounts: net balances after "
        f"create→post→reverse differ from before.\nbefore={before}\nafter={after}"
    )


async def test_reversal_in_later_period_keeps_original_live(
    api_client: AsyncClient,
) -> None:
    """Period-boundary caveat: a reversal dated in a LATER period must NOT
    erase the original from a trial balance taken BETWEEN the two dates.

    Original posted period A (2028-03), reversed period B (2028-05). As-of end
    of A the original is still live; as-of end of B it has netted to zero. This
    is exactly why option (a) (include REVERSED, let the date filter drop the
    later reversal) is correct and option (b) (drop both sides regardless of
    date) would be wrong.
    """
    asset_id = await _make_account(api_client, AccountType.ASSET)
    income_id = await _make_account(api_client, AccountType.INCOME)
    owned = {asset_id, income_id}

    end_a = "2028-03-31"
    end_b = "2028-05-31"

    before_a = await _net_balances(api_client, end_a, owned)
    before_b = await _net_balances(api_client, end_b, owned)
    assert before_a == {asset_id: 0.0, income_id: 0.0}, before_a

    je = await _create_and_post_je(
        api_client,
        "2028-03-15",
        lines=[
            {"account_id": asset_id, "debit": "2640.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "2640.00"},
        ],
    )
    after_post_a = await _net_balances(api_client, end_a, owned)
    assert after_post_a == {asset_id: 2640.0, income_id: -2640.0}, after_post_a

    await _reverse_je(api_client, je, reversal_date="2028-05-15")

    # As-of end of period A: reversal is in the future → original still live.
    still_live_a = await _net_balances(api_client, end_a, owned)
    assert still_live_a == after_post_a, (
        "a later-dated reversal must NOT retroactively erase the original from "
        f"period A.\nexpected (post)={after_post_a}\ngot={still_live_a}"
    )

    # As-of end of period B: original + reversal in scope → net zero.
    netted_b = await _net_balances(api_client, end_b, owned)
    assert netted_b == before_b, (
        "by end of period B the reversal should net the original to zero.\n"
        f"before={before_b}\nnetted={netted_b}"
    )
