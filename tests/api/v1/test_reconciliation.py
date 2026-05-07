"""Phase 1 cycle 42 contract tests for /api/v1/reconciliation.

Covers:
* Auth gate (401 without bearer)
* GET /reconciliation/accounts → 200, list shape
* GET /reconciliation/unmatched → 200, empty list initially; only UNMATCHED returned
* GET /reconciliation/suggest/{bsl_id} → 200, list of candidates; 404 for unknown BSL
* POST /reconciliation/match → 200, BSL status=MATCHED; 404 for unknown BSL; 422 for
  unposted entry
* POST /reconciliation/unmatch/{bsl_id} → 200, BSL status=UNMATCHED; 404 for unknown
* POST /reconciliation/auto_match → 200, {"matched": N}
* Tenant isolation: BSL belonging to a different company is not visible
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine


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
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def bank_account_id(api_client: AsyncClient) -> str:
    """Create a bank account via API and return its ID."""
    payload = {
        "code": f"REC-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Reconciliation Bank",
        "bsb": "062-001",
        "bank_account_number": "11223344",
        "bank_account_title": "Recon Test Account",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def unmatched_bsl_id(api_client: AsyncClient, bank_account_id: str) -> str:
    """Create an UNMATCHED bank statement line and return its ID."""
    payload = {
        "account_id": bank_account_id,
        "txn_date": "2026-04-01",
        "amount": "250.00",
        "description": "Recon test deposit",
        "status": "UNMATCHED",
    }
    r = await api_client.post("/api/v1/bank_statement_lines", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _get_company_id() -> uuid.UUID:
    """Return the first active company ID (mirrors the API helper)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
    assert company is not None
    return company.id


async def _get_expense_account_id() -> uuid.UUID:
    """Return an EXPENSE account ID."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Account).where(
                Account.archived_at.is_(None),
                Account.account_type == AccountType.EXPENSE,
            ).limit(1)
        )
        account = result.scalars().first()
    assert account is not None, "Test DB has no EXPENSE account"
    return account.id


@pytest.fixture
async def posted_entry_for_bsl(
    unmatched_bsl_id: str,
    bank_account_id: str,
) -> str:
    """Create a POSTED journal entry whose bank line matches the BSL amount (250.00 debit).

    The BSL has amount=250.00 (deposit), so the matching entry needs a debit of
    250.00 to the bank account.
    """
    company_id = await _get_company_id()
    expense_id = await _get_expense_account_id()

    async with AsyncSessionLocal() as session:
        # Get the account row for the bank_account_id
        bank_acc = await session.get(Account, uuid.UUID(bank_account_id))
        assert bank_acc is not None

        entry = JournalEntry(
            id=uuid.uuid4(),
            company_id=company_id,
            ref=f"RECON-{uuid.uuid4().hex[:6].upper()}",
            entry_date=__import__("datetime").date(2026, 4, 1),
            description="Recon test posted entry",
            status=EntryStatus.POSTED,
        )
        session.add(entry)
        await session.flush()

        # Debit bank account (positive=deposit in BSL → debit in JE)
        session.add(JournalLine(
            entry_id=entry.id,
            line_no=1,
            account_id=uuid.UUID(bank_account_id),
            debit=Decimal("250.00"),
            credit=Decimal("0"),
        ))
        # Credit expense
        session.add(JournalLine(
            entry_id=entry.id,
            line_no=2,
            account_id=expense_id,
            debit=Decimal("0"),
            credit=Decimal("250.00"),
        ))
        await session.commit()
        return str(entry.id)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_reconciliation_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/reconciliation/accounts")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /reconciliation/accounts
# ---------------------------------------------------------------------------


async def test_reconciliation_accounts_200(api_client: AsyncClient) -> None:
    """GET /accounts returns a list (may be empty or non-empty)."""
    r = await api_client.get("/api/v1/reconciliation/accounts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    # If any accounts returned they must have expected keys
    for account in body:
        assert "id" in account
        assert "code" in account
        assert "name" in account


async def test_reconciliation_accounts_contains_bank_account(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A bank account created via API should appear in the reconciliation accounts list."""
    r = await api_client.get("/api/v1/reconciliation/accounts")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()]
    assert bank_account_id in ids


# ---------------------------------------------------------------------------
# GET /reconciliation/unmatched
# ---------------------------------------------------------------------------


async def test_reconciliation_unmatched_empty(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """An account with no BSLs returns an empty list of unmatched lines."""
    # Create a fresh account with no lines
    fresh_payload = {
        "code": f"FRESH-{uuid.uuid4().hex[:6].upper()}",
        "name": "Fresh Recon Account",
        "bsb": "063-999",
        "bank_account_number": "55667788",
        "bank_account_title": "Fresh",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=fresh_payload)
    assert r.status_code == 201
    fresh_id = r.json()["id"]

    r2 = await api_client.get(
        "/api/v1/reconciliation/unmatched", params={"account_id": fresh_id}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == []


async def test_reconciliation_unmatched_with_data(
    api_client: AsyncClient, bank_account_id: str, unmatched_bsl_id: str
) -> None:
    """Unmatched endpoint returns BSLs for the account that have UNMATCHED status."""
    r = await api_client.get(
        "/api/v1/reconciliation/unmatched", params={"account_id": bank_account_id}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    ids = [item["id"] for item in body]
    assert unmatched_bsl_id in ids
    # All returned items must be UNMATCHED
    for item in body:
        assert item["status"] == "UNMATCHED"


async def test_reconciliation_unmatched_excludes_matched(
    api_client: AsyncClient, bank_account_id: str, unmatched_bsl_id: str,
    posted_entry_for_bsl: str,
) -> None:
    """After matching, the BSL no longer appears in the unmatched list."""
    # Match the line first
    r = await api_client.post(
        "/api/v1/reconciliation/match",
        json={"bsl_id": unmatched_bsl_id, "entry_id": posted_entry_for_bsl},
    )
    assert r.status_code == 200, r.text

    r2 = await api_client.get(
        "/api/v1/reconciliation/unmatched", params={"account_id": bank_account_id}
    )
    assert r2.status_code == 200
    ids = [item["id"] for item in r2.json()]
    assert unmatched_bsl_id not in ids


# ---------------------------------------------------------------------------
# GET /reconciliation/suggest/{bsl_id}
# ---------------------------------------------------------------------------


async def test_reconciliation_suggest_404_unknown_bsl(api_client: AsyncClient) -> None:
    """suggest returns 404 for an unknown BSL ID."""
    r = await api_client.get(f"/api/v1/reconciliation/suggest/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_reconciliation_suggest_returns_list(
    api_client: AsyncClient, unmatched_bsl_id: str
) -> None:
    """suggest returns a list (empty is fine — no matching posted entries yet)."""
    r = await api_client.get(f"/api/v1/reconciliation/suggest/{unmatched_bsl_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)


async def test_reconciliation_suggest_returns_candidate(
    api_client: AsyncClient, unmatched_bsl_id: str, posted_entry_for_bsl: str
) -> None:
    """suggest returns the posted entry that matches the BSL amount."""
    r = await api_client.get(f"/api/v1/reconciliation/suggest/{unmatched_bsl_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    ids = [e["id"] for e in body]
    assert posted_entry_for_bsl in ids
    # Each entry has expected keys
    for entry in body:
        assert "id" in entry
        assert "ref" in entry
        assert "entry_date" in entry
        assert "status" in entry
        assert entry["status"] == "POSTED"


# ---------------------------------------------------------------------------
# POST /reconciliation/match
# ---------------------------------------------------------------------------


async def test_reconciliation_match_200(
    api_client: AsyncClient, unmatched_bsl_id: str, posted_entry_for_bsl: str
) -> None:
    """POST /match sets BSL to MATCHED and records entry ID."""
    r = await api_client.post(
        "/api/v1/reconciliation/match",
        json={"bsl_id": unmatched_bsl_id, "entry_id": posted_entry_for_bsl},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == unmatched_bsl_id
    assert body["status"] == "MATCHED"
    assert body["matched_entry_id"] == posted_entry_for_bsl
    assert body["matched_at"] is not None


async def test_reconciliation_match_404_unknown_bsl(api_client: AsyncClient) -> None:
    """POST /match with unknown bsl_id returns 404."""
    r = await api_client.post(
        "/api/v1/reconciliation/match",
        json={"bsl_id": str(uuid.uuid4()), "entry_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


async def test_reconciliation_match_422_unposted_entry(
    api_client: AsyncClient, unmatched_bsl_id: str
) -> None:
    """POST /match against a DRAFT journal entry returns 422."""
    company_id = await _get_company_id()
    expense_id = await _get_expense_account_id()

    # Create a DRAFT entry (not posted)
    async with AsyncSessionLocal() as session:
        draft_entry = JournalEntry(
            id=uuid.uuid4(),
            company_id=company_id,
            ref=f"DRAFT-{uuid.uuid4().hex[:6].upper()}",
            entry_date=__import__("datetime").date(2026, 4, 1),
            status=EntryStatus.DRAFT,
        )
        session.add(draft_entry)
        await session.commit()
        draft_entry_id = str(draft_entry.id)

    r = await api_client.post(
        "/api/v1/reconciliation/match",
        json={"bsl_id": unmatched_bsl_id, "entry_id": draft_entry_id},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# POST /reconciliation/unmatch/{bsl_id}
# ---------------------------------------------------------------------------


async def test_reconciliation_unmatch_200(
    api_client: AsyncClient, unmatched_bsl_id: str, posted_entry_for_bsl: str
) -> None:
    """POST /unmatch returns BSL to UNMATCHED and clears matched_entry_id."""
    # First match it
    r = await api_client.post(
        "/api/v1/reconciliation/match",
        json={"bsl_id": unmatched_bsl_id, "entry_id": posted_entry_for_bsl},
    )
    assert r.status_code == 200

    # Now unmatch
    r2 = await api_client.post(f"/api/v1/reconciliation/unmatch/{unmatched_bsl_id}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["id"] == unmatched_bsl_id
    assert body["status"] == "UNMATCHED"
    assert body["matched_entry_id"] is None
    assert body["matched_at"] is None


async def test_reconciliation_unmatch_404_unknown_bsl(api_client: AsyncClient) -> None:
    """POST /unmatch with unknown BSL ID returns 404."""
    r = await api_client.post(f"/api/v1/reconciliation/unmatch/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /reconciliation/auto_match
# ---------------------------------------------------------------------------


async def test_reconciliation_auto_match_returns_count(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """POST /auto_match returns {"matched": N} for an account with no unmatched lines."""
    # Use a fresh account — no unmatched lines
    fresh_payload = {
        "code": f"AUTO-{uuid.uuid4().hex[:6].upper()}",
        "name": "Auto Match Test Account",
        "bsb": "064-001",
        "bank_account_number": "99112233",
        "bank_account_title": "Auto",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=fresh_payload)
    assert r.status_code == 201
    fresh_id = r.json()["id"]

    r2 = await api_client.post(
        "/api/v1/reconciliation/auto_match", params={"account_id": fresh_id}
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "matched" in body
    assert isinstance(body["matched"], int)
    assert body["matched"] == 0


async def test_reconciliation_auto_match_matches_line(
    api_client: AsyncClient,
    unmatched_bsl_id: str,
    bank_account_id: str,
    posted_entry_for_bsl: str,
) -> None:
    """POST /auto_match matches the unmatched BSL to the candidate entry."""
    r = await api_client.post(
        "/api/v1/reconciliation/auto_match", params={"account_id": bank_account_id}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] >= 1

    # Verify the BSL is now MATCHED
    async with AsyncSessionLocal() as session:
        bsl = await session.get(BankStatementLine, uuid.UUID(unmatched_bsl_id))
    assert bsl is not None
    assert bsl.status == StatementLineStatus.MATCHED
    assert bsl.matched_entry_id == uuid.UUID(posted_entry_for_bsl)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /bank_statement_lines/{id}/split_match  (regression)
# ---------------------------------------------------------------------------


async def test_split_match_deposit_200(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Split-match: $970 deposit = $1000 AR credit + $30 fee debit."""
    # Create a $970 BSL
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-15",
            "amount": "970.00",
            "description": "Stripe payout",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    expense_id = str(await _get_expense_account_id())
    ar_id = str(await _get_ar_account_id())

    r2 = await api_client.post(
        f"/api/v1/bank_statement_lines/{bsl_id}/split_match",
        json={
            "description": "Stripe net payout split",
            "entry_date": "2026-04-15",
            "allocations": [
                {"account_id": ar_id, "credit": "1000.00", "debit": "0"},
                {"account_id": expense_id, "debit": "30.00", "credit": "0"},
            ],
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["id"] == bsl_id
    assert body["status"] == "MATCHED"
    assert body["matched_entry_id"] is not None
    assert body["matched_to_type"] == "JOURNAL_ENTRY"


async def test_split_match_withdrawal_200(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Split-match: -$110 withdrawal = $100 expense + $10 GST (split debit)."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-16",
            "amount": "-110.00",
            "description": "Supplier payment",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    expense_id = str(await _get_expense_account_id())
    ar_id = str(await _get_ar_account_id())

    r2 = await api_client.post(
        f"/api/v1/bank_statement_lines/{bsl_id}/split_match",
        json={
            "description": "Supplier split",
            "entry_date": "2026-04-16",
            "allocations": [
                {"account_id": expense_id, "debit": "100.00", "credit": "0"},
                {"account_id": ar_id, "debit": "10.00", "credit": "0"},
            ],
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "MATCHED"


async def test_split_match_422_imbalanced(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Split-match rejects allocations that don't sum to the bank line amount."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-17",
            "amount": "500.00",
            "description": "Balance test",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    expense_id = str(await _get_expense_account_id())

    r2 = await api_client.post(
        f"/api/v1/bank_statement_lines/{bsl_id}/split_match",
        json={
            "allocations": [
                {"account_id": expense_id, "credit": "400.00", "debit": "0"},
            ],
        },
    )
    assert r2.status_code == 422, r2.text


async def test_split_match_404_unknown_bsl(api_client: AsyncClient) -> None:
    """Split-match returns 404 for an unknown BSL ID."""
    expense_id = str(await _get_expense_account_id())
    r = await api_client.post(
        f"/api/v1/bank_statement_lines/{uuid.uuid4()}/split_match",
        json={
            "allocations": [
                {"account_id": expense_id, "credit": "100.00", "debit": "0"},
            ],
        },
    )
    assert r.status_code == 404, r.text


async def _get_ar_account_id() -> uuid.UUID:
    """Return an ASSET account that is not a bank account (for AR-like use)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Account).where(
                Account.archived_at.is_(None),
                Account.account_type == AccountType.ASSET,
                Account.reconcile.is_(False),
            ).limit(1)
        )
        account = result.scalars().first()
    if account is None:
        # Fall back to any asset account
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
            account = result.scalars().first()
    assert account is not None, "Test DB has no ASSET account"
    return account.id


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_reconciliation_tenant_isolation_suggest(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A BSL belonging to a second company returns 404 from the suggest endpoint."""
    foreign_company_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        # Get any account for the FK (belongs to first company but that's
        # fine — we just need a valid account_id for the BSL row)
        account = (
            await session.execute(
                select(Account).where(Account.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        assert account is not None

        second_company = Company(
            id=foreign_company_id,
            name="Recon Isolation Co",
            base_currency="AUD",
            fin_year_start_month=7,
            audit_mode="immutable",
        )
        session.add(second_company)
        await session.flush()

        foreign_bsl = BankStatementLine(
            id=uuid.uuid4(),
            company_id=foreign_company_id,
            account_id=account.id,
            txn_date=__import__("datetime").date(2026, 4, 1),
            amount=Decimal("100.00"),
            status=StatementLineStatus.UNMATCHED,
        )
        session.add(foreign_bsl)
        await session.commit()
        foreign_bsl_id = str(foreign_bsl.id)

    try:
        # Suggest should 404 — the BSL belongs to a different company
        r = await api_client.get(f"/api/v1/reconciliation/suggest/{foreign_bsl_id}")
        assert r.status_code == 404
    finally:
        # Cleanup
        async with AsyncSessionLocal() as session:
            bsl_obj = await session.get(BankStatementLine, uuid.UUID(foreign_bsl_id))
            if bsl_obj is not None:
                await session.delete(bsl_obj)
                await session.flush()
            co = await session.get(Company, foreign_company_id)
            if co is not None:
                await session.delete(co)
            await session.commit()
