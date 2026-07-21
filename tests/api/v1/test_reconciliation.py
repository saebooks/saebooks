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
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

pytestmark = pytest.mark.postgres_only


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
    """Return an EXPENSE account ID from the SAME company _get_company_id()
    returns — the multi-jurisdiction seed now carries more than one company, so
    an unscoped pick can return a foreign-company account and violate the
    journal_lines (account_id, company_id) FK."""
    company_id = await _get_company_id()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.archived_at.is_(None),
                Account.account_type == AccountType.EXPENSE,
                Account.is_header.is_(False),
            ).order_by(Account.code).limit(1)
        )
        account = result.scalars().first()
    assert account is not None, "Test DB has no EXPENSE account"
    return account.id


@pytest.fixture
async def posted_entry_for_bsl(
    unmatched_bsl_id: str,
    bank_account_id: str,
):
    """Create a POSTED journal entry whose bank line matches the BSL amount (250.00 debit).

    The BSL has amount=250.00 (deposit), so the matching entry needs a debit of
    250.00 to the bank account.

    Teardown deletes the JournalEntry (its lines cascade via
    ``journal_lines.entry_id`` ON DELETE CASCADE) — without this the credit
    side leaks a permanent -250.00 into the shared default company's expense
    account for any report covering 2026-04-01, exactly the residue pattern
    this branch exists to remove.
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
            tenant_id=DEFAULT_TENANT_ID,
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
        entry_id = entry.id

    try:
        yield str(entry_id)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(JournalEntry).where(JournalEntry.id == entry_id))
            await session.commit()


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


async def test_reconciliation_unmatched_pagination(
    api_client: AsyncClient,
) -> None:
    """limit/offset page through the unmatched set in txn_date order; the
    body stays a bare array and X-Total-Count carries the unpaginated total."""
    payload = {
        "code": f"PAG-{uuid.uuid4().hex[:8].upper()}",
        "name": "Pagination Recon Account",
        "bsb": "064-111",
        "bank_account_number": "99887766",
        "bank_account_title": "Pagination",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]

    for day in range(1, 6):
        r = await api_client.post(
            "/api/v1/bank_statement_lines",
            json={
                "account_id": account_id,
                "txn_date": f"2026-05-{day:02d}",
                "amount": f"{day}.00",
                "description": f"Pagination line {day}",
                "status": "UNMATCHED",
            },
        )
        assert r.status_code == 201, r.text

    # Legacy shape: no limit → full set, bare array, total header present
    r = await api_client.get(
        "/api/v1/reconciliation/unmatched", params={"account_id": account_id}
    )
    assert r.status_code == 200, r.text
    full = r.json()
    assert isinstance(full, list)
    assert len(full) == 5
    assert r.headers["X-Total-Count"] == "5"

    # First page
    r = await api_client.get(
        "/api/v1/reconciliation/unmatched",
        params={"account_id": account_id, "limit": 2},
    )
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert [item["id"] for item in page1] == [item["id"] for item in full[:2]]
    assert r.headers["X-Total-Count"] == "5"

    # Second page
    r = await api_client.get(
        "/api/v1/reconciliation/unmatched",
        params={"account_id": account_id, "limit": 2, "offset": 2},
    )
    assert r.status_code == 200, r.text
    page2 = r.json()
    assert [item["id"] for item in page2] == [item["id"] for item in full[2:4]]

    # Offset past the end → empty page, total unchanged
    r = await api_client.get(
        "/api/v1/reconciliation/unmatched",
        params={"account_id": account_id, "limit": 2, "offset": 10},
    )
    assert r.status_code == 200, r.text
    assert r.json() == []
    assert r.headers["X-Total-Count"] == "5"


async def test_reconciliation_unmatched_pagination_validation(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Out-of-range limit/offset values are rejected with 422."""
    for params in (
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
    ):
        r = await api_client.get(
            "/api/v1/reconciliation/unmatched",
            params={"account_id": bank_account_id, **params},
        )
        assert r.status_code == 422, (params, r.text)


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
        # R8a — additive scoring fields on every candidate.
        assert "confidence" in entry
        assert "match_reason" in entry
        assert "rule_id" in entry


# ---------------------------------------------------------------------------
# R8a — suggest confidence scoring
# ---------------------------------------------------------------------------


async def test_reconciliation_suggest_scores_amount_and_date_medium(
    api_client: AsyncClient, unmatched_bsl_id: str, posted_entry_for_bsl: str
) -> None:
    """Same-date, no-reference candidate scores MEDIUM / AMOUNT_AND_DATE.

    ``posted_entry_for_bsl`` shares the BSL's txn_date (2026-04-01) with no
    reference overlap and no bank rule in play — the canonical
    AMOUNT_AND_DATE fixture.
    """
    r = await api_client.get(f"/api/v1/reconciliation/suggest/{unmatched_bsl_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    match = next(e for e in body if e["id"] == posted_entry_for_bsl)
    assert match["confidence"] == "MEDIUM"
    assert match["match_reason"] == "AMOUNT_AND_DATE"
    assert match["rule_id"] is None


async def test_reconciliation_suggest_scores_exact_amount_low(
    api_client: AsyncClient, bank_account_id: str, posted_entry_factory,
) -> None:
    """A candidate with a distant date and no reference overlap scores LOW /
    EXACT_AMOUNT — the weakest tier, amount agreement only."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-10",
            "amount": "333.00",
            "description": "Miscellaneous receipt",
            "status": "UNMATCHED",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    entry_id = await posted_entry_factory(
        account_id=bank_account_id,
        amount=Decimal("333.00"),
        entry_date=date(2026, 6, 15),  # far outside the date-proximity window
        ref=f"FARDATE-{uuid.uuid4().hex[:6].upper()}",
        description="Unrelated posting",
    )

    r2 = await api_client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")
    assert r2.status_code == 200, r2.text
    match = next(e for e in r2.json() if e["id"] == entry_id)
    assert match["confidence"] == "LOW"
    assert match["match_reason"] == "EXACT_AMOUNT"
    assert match["rule_id"] is None


async def test_reconciliation_suggest_scores_reference_overlap_high(
    api_client: AsyncClient, bank_account_id: str, posted_entry_factory,
) -> None:
    """A candidate whose description/ref contains the BSL's reference scores
    HIGH / AMOUNT_AND_REFERENCE, even with a far-off date."""
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-11",
            "amount": "888.00",
            "description": "Settlement",
            "reference": "REFHIGH-9999",
            "status": "UNMATCHED",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    entry_id = await posted_entry_factory(
        account_id=bank_account_id,
        amount=Decimal("888.00"),
        entry_date=date(2026, 7, 1),
        ref="REFHIGH-9999",
        description="Payment against REFHIGH-9999",
    )

    r2 = await api_client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")
    assert r2.status_code == 200, r2.text
    match = next(e for e in r2.json() if e["id"] == entry_id)
    assert match["confidence"] == "HIGH"
    assert match["match_reason"] == "AMOUNT_AND_REFERENCE"
    assert match["rule_id"] is None


async def test_reconciliation_suggest_scores_rule_pattern_high(
    api_client: AsyncClient, bank_account_id: str, posted_entry_factory,
) -> None:
    """A bank rule matching the BSL description scores HIGH / RULE_PATTERN
    with ``rule_id`` set — the most specific signal, outranking a plain
    reference overlap."""
    from saebooks.services import bank_rules as bank_rules_svc

    company_id = await _get_company_id()
    expense_id = await _get_expense_account_id()

    unique_pattern = f"RULEPAT-{uuid.uuid4().hex[:8].upper()}"
    async with AsyncSessionLocal() as session:
        rule = await bank_rules_svc.create(
            session,
            company_id,
            name=f"R8a rule {unique_pattern}",
            match_pattern=unique_pattern,
            account_id=expense_id,
        )
        rule_id = str(rule.id)

    try:
        r = await api_client.post(
            "/api/v1/bank_statement_lines",
            json={
                "account_id": bank_account_id,
                "txn_date": "2026-04-12",
                "amount": "222.00",
                "description": f"{unique_pattern} monthly charge",
                "status": "UNMATCHED",
            },
        )
        assert r.status_code == 201, r.text
        bsl_id = r.json()["id"]

        entry_id = await posted_entry_factory(
            account_id=bank_account_id,
            amount=Decimal("222.00"),
            entry_date=date(2026, 9, 1),
            ref=f"NOOVERLAP-{uuid.uuid4().hex[:6].upper()}",
            description="Unrelated description",
        )

        r2 = await api_client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")
        assert r2.status_code == 200, r2.text
        match = next(e for e in r2.json() if e["id"] == entry_id)
        assert match["confidence"] == "HIGH"
        assert match["match_reason"] == "RULE_PATTERN"
        assert match["rule_id"] == rule_id
    finally:
        # This file's own BankRule leak — the seeded default company
        # otherwise accumulates one throwaway rule per run forever.
        from saebooks.models.bank_rule import BankRule
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(BankRule).where(BankRule.id == uuid.UUID(rule_id)))
            await session.commit()


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
    await _get_expense_account_id()

    # Create a DRAFT entry (not posted)
    async with AsyncSessionLocal() as session:
        draft_entry = JournalEntry(
            id=uuid.uuid4(),
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
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


async def test_reconciliation_auto_match_skips_medium_confidence_candidate(
    api_client: AsyncClient,
    unmatched_bsl_id: str,
    bank_account_id: str,
    posted_entry_for_bsl: str,
) -> None:
    """R8d — a same-date, no-reference candidate scores MEDIUM (AMOUNT_AND_DATE),
    not HIGH, so auto_match does NOT link it. This inverts the pre-R8 greedy
    behaviour deliberately (see design note R8d) — auto_match only links when
    exactly one candidate scores HIGH confidence.
    """
    r = await api_client.post(
        "/api/v1/reconciliation/auto_match", params={"account_id": bank_account_id}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 0
    assert body["skipped_no_candidate"] >= 1

    # BSL stays UNMATCHED — a MEDIUM-confidence candidate is not enough.
    async with AsyncSessionLocal() as session:
        bsl = await session.get(BankStatementLine, uuid.UUID(unmatched_bsl_id))
    assert bsl is not None
    assert bsl.status == StatementLineStatus.UNMATCHED
    assert bsl.matched_entry_id is None


async def _create_posted_entry_for_line(
    *,
    account_id: str,
    amount: Decimal,
    entry_date,
    ref: str,
    description: str,
) -> str:
    """Create a POSTED journal entry whose bank-account leg matches ``amount``
    (positive=debit/deposit, negative=credit/withdrawal) at ``account_id``.
    """
    company_id = await _get_company_id()
    expense_id = await _get_expense_account_id()

    async with AsyncSessionLocal() as session:
        entry = JournalEntry(
            id=uuid.uuid4(),
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            ref=ref,
            entry_date=entry_date,
            description=description,
            status=EntryStatus.POSTED,
        )
        session.add(entry)
        await session.flush()

        if amount >= 0:
            bank_debit, bank_credit = abs(amount), Decimal("0")
            other_debit, other_credit = Decimal("0"), abs(amount)
        else:
            bank_debit, bank_credit = Decimal("0"), abs(amount)
            other_debit, other_credit = abs(amount), Decimal("0")

        session.add(JournalLine(
            entry_id=entry.id, line_no=1, account_id=uuid.UUID(account_id),
            debit=bank_debit, credit=bank_credit,
        ))
        session.add(JournalLine(
            entry_id=entry.id, line_no=2, account_id=expense_id,
            debit=other_debit, credit=other_credit,
        ))
        await session.commit()
        return str(entry.id)


@pytest.fixture
async def posted_entry_factory():
    """Factory wrapping ``_create_posted_entry_for_line`` with teardown.

    Every one of this file's ``_create_posted_entry_for_line`` call sites
    posts a real credit (or debit) to the shared default company's EXPENSE
    account, several dated in real-looking months (2026-05, 2026-06, etc)
    that other tests/report queries scan over. None of the 5 call sites
    tore the entry down, so the credit side permanently skewed that
    account's balance for any report covering the same window (confirmed:
    this is what broke ``test_reports_financial.py::test_pnl_expense_line``
    when this file ran in the same session). Delete the JournalEntry on
    teardown — its lines cascade via ``journal_lines.entry_id`` ON DELETE
    CASCADE.
    """
    created: list[uuid.UUID] = []

    async def _make(**kwargs) -> str:
        entry_id = await _create_posted_entry_for_line(**kwargs)
        created.append(uuid.UUID(entry_id))
        return entry_id

    try:
        yield _make
    finally:
        if created:
            async with AsyncSessionLocal() as session:
                await session.execute(sa_delete(JournalEntry).where(JournalEntry.id.in_(created)))
                await session.commit()


async def test_reconciliation_auto_match_matches_high_confidence_reference(
    api_client: AsyncClient, bank_account_id: str, posted_entry_factory,
) -> None:
    """A candidate whose ref/description contains the BSL's reference scores
    HIGH (AMOUNT_AND_REFERENCE) and, being the ONLY high-confidence candidate,
    gets auto-matched.
    """
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-20",
            "amount": "777.00",
            "description": "Settlement",
            "reference": "INV-9001",
            "status": "UNMATCHED",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    entry_id = await posted_entry_factory(
        account_id=bank_account_id,
        amount=Decimal("777.00"),
        entry_date=date(2026, 5, 1),  # deliberately NOT date-proximate
        ref="INV-9001",
        description="Payment against INV-9001",
    )

    r2 = await api_client.post(
        "/api/v1/reconciliation/auto_match", params={"account_id": bank_account_id}
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["matched"] >= 1

    async with AsyncSessionLocal() as session:
        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
    assert bsl is not None
    assert bsl.status == StatementLineStatus.MATCHED
    assert bsl.matched_entry_id == uuid.UUID(entry_id)


async def test_reconciliation_auto_match_skips_ambiguous_high_confidence_candidates(
    api_client: AsyncClient, bank_account_id: str, posted_entry_factory,
) -> None:
    """Two candidates both score HIGH (same reference overlap) → ambiguous,
    skipped and counted, never guessed at.
    """
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": "2026-04-21",
            "amount": "444.00",
            "description": "Settlement",
            "reference": "INV-9002",
            "status": "UNMATCHED",
        },
    )
    assert r.status_code == 201, r.text
    bsl_id = r.json()["id"]

    for suffix in ("A", "B"):
        await posted_entry_factory(
            account_id=bank_account_id,
            amount=Decimal("444.00"),
            entry_date=date(2026, 5, 2),
            ref=f"AMB-{suffix}-{uuid.uuid4().hex[:6].upper()}",
            description="Payment against INV-9002",
        )

    r2 = await api_client.post(
        "/api/v1/reconciliation/auto_match", params={"account_id": bank_account_id}
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["matched"] == 0
    assert body["skipped_ambiguous"] >= 1

    async with AsyncSessionLocal() as session:
        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
    assert bsl is not None
    assert bsl.status == StatementLineStatus.UNMATCHED


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /bank_statement_lines/{id}/split_match  (ETSY-4)
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
    """Return an ASSET account that is not a bank account (for AR-like use),
    scoped to the SAME company _get_company_id() returns (see
    _get_expense_account_id — foreign-company accounts break the FK)."""
    company_id = await _get_company_id()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.archived_at.is_(None),
                Account.account_type == AccountType.ASSET,
                Account.is_header.is_(False),
                Account.reconcile.is_(False),
            ).order_by(Account.code).limit(1)
        )
        account = result.scalars().first()
    if account is None:
        # Fall back to any asset account in the same company
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                ).order_by(Account.code).limit(1)
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
