"""Contract tests for POST /api/v1/reconciliation/create_and_match (M3 R8c).

Covers:
* Auth gate (401 without bearer)
* expense spec — create + post + match in one call, 201, BSL MATCHED,
  matched_via=COMPOUND
* payment spec — same, for a deposit (INCOMING direction inferred from
  a positive bank line)
* record_type validation — unknown record_type → 422; missing spec for the
  chosen record_type → 422 (caught at the pydantic layer)
* expense on a deposit (positive BSL amount) → 422 (expenses only debit spend)
* amount-mismatch atomicity — expense line totals that don't reconcile with
  the bank line amount roll back fully; NOTHING persists (no expense row)
* match-step atomicity — a forced failure in the final add_match leaves the
  posted record intact but NO bsl_matches row and the BSL stays UNMATCHED
  (see services/reconciliation.create_and_match's atomicity docstring —
  full single-transaction rollback across create+post+match is not achieved
  by this slice; this test locks in the residual behaviour instead)
* post-step atomicity — a forced failure inside api_post_expense (the
  posting phase, a DISTINCT code path from the match-step failure above)
  leaves a DRAFT expense with no journal entry and no match; the BSL stays
  UNMATCHED and no bsl_matches row is created
* 404 for an unknown / cross-tenant BSL
* 422 for an already-MATCHED BSL
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.bsl_match import BslMatch
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.expense import Expense, ExpenseStatus
from saebooks.models.payment import Payment
from saebooks.services import expenses as expenses_svc_module
from saebooks.services import reconciliation as svc

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


async def _get_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
    assert company is not None
    return company.id


async def _get_expense_account_id() -> uuid.UUID:
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


async def _get_contact_id() -> uuid.UUID:
    company_id = await _get_company_id()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.archived_at.is_(None),
            ).limit(1)
        )
        contact = result.scalars().first()
    assert contact is not None, "Test DB has no contact"
    return contact.id


@pytest.fixture
async def bank_account_id(api_client: AsyncClient) -> str:
    payload = {
        "code": f"CAM-{uuid.uuid4().hex[:8].upper()}",
        "name": "Create-And-Match Test Account",
        "bsb": "062-002",
        "bank_account_number": str(uuid.uuid4().int)[:8],
        "bank_account_title": "Create And Match Test",
    }
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_bsl(
    api_client: AsyncClient, bank_account_id: str, *, amount: str, txn_date: str = "2026-05-01",
    description: str = "Create-and-match test line",
) -> str:
    r = await api_client.post(
        "/api/v1/bank_statement_lines",
        json={
            "account_id": bank_account_id,
            "txn_date": txn_date,
            "amount": amount,
            "description": description,
            "status": "UNMATCHED",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_create_and_match_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={"bsl_id": str(uuid.uuid4()), "record_type": "expense", "expense": {"lines": []}},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Expense spec — happy path
# ---------------------------------------------------------------------------


async def test_create_and_match_expense_success(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A withdrawal BSL + a reconciling expense spec → 201, expense POSTED,
    BSL MATCHED, matched_via=COMPOUND."""
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="-150.00")
    expense_account_id = str(await _get_expense_account_id())

    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "expense",
            "expense": {
                "lines": [
                    {
                        "description": "Office supplies",
                        "account_id": expense_account_id,
                        "quantity": "1",
                        "unit_price": "150.00",
                    }
                ],
            },
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["record_type"] == "expense"
    assert body["bsl"]["status"] == "MATCHED"
    assert body["bsl"]["id"] == bsl_id
    assert body["journal_entry_id"]
    assert body["record_id"]

    async with AsyncSessionLocal() as session:
        expense = await session.get(Expense, uuid.UUID(body["record_id"]))
        assert expense is not None
        assert expense.status.value == "POSTED"

        match = (
            await session.execute(
                select(BslMatch).where(
                    BslMatch.bsl_id == uuid.UUID(bsl_id),
                    BslMatch.archived_at.is_(None),
                )
            )
        ).scalars().first()
        assert match is not None
        assert match.matched_via == "COMPOUND"
        assert match.target_id == uuid.UUID(body["journal_entry_id"])


async def test_create_and_match_expense_rejects_deposit(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """An expense spec against a deposit (positive amount) is rejected — 422."""
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="150.00")
    expense_account_id = str(await _get_expense_account_id())

    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "expense",
            "expense": {
                "lines": [
                    {
                        "description": "Office supplies",
                        "account_id": expense_account_id,
                        "quantity": "1",
                        "unit_price": "150.00",
                    }
                ],
            },
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Payment spec — happy path
# ---------------------------------------------------------------------------


async def test_create_and_match_payment_success(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A deposit BSL + a payment spec → 201, payment POSTED (INCOMING),
    BSL MATCHED, matched_via=COMPOUND."""
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="500.00")
    contact_id = str(await _get_contact_id())

    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "payment",
            "payment": {"contact_id": contact_id},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["record_type"] == "payment"
    assert body["bsl"]["status"] == "MATCHED"

    async with AsyncSessionLocal() as session:
        match = (
            await session.execute(
                select(BslMatch).where(
                    BslMatch.bsl_id == uuid.UUID(bsl_id),
                    BslMatch.archived_at.is_(None),
                )
            )
        ).scalars().first()
        assert match is not None
        assert match.matched_via == "COMPOUND"
        assert match.amount == Decimal("500.00")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_create_and_match_unknown_record_type_422(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="-50.00")
    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={"bsl_id": bsl_id, "record_type": "journal_entry"},
    )
    assert r.status_code == 422, r.text


async def test_create_and_match_missing_spec_422(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """record_type=expense with no 'expense' spec → 422 (pydantic validator)."""
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="-50.00")
    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={"bsl_id": bsl_id, "record_type": "expense"},
    )
    assert r.status_code == 422, r.text


async def test_create_and_match_404_unknown_bsl(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": str(uuid.uuid4()),
            "record_type": "payment",
            "payment": {"contact_id": str(uuid.uuid4())},
        },
    )
    assert r.status_code == 404, r.text


async def test_create_and_match_422_already_matched(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="500.00")
    contact_id = str(await _get_contact_id())

    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "payment",
            "payment": {"contact_id": contact_id},
        },
    )
    assert r.status_code == 201, r.text

    r2 = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "payment",
            "payment": {"contact_id": contact_id},
        },
    )
    assert r2.status_code == 422, r2.text


async def test_create_and_match_422_partial_line(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A PARTIAL line is rejected: create_and_match builds the record for the
    FULL bsl.amount, which would always over-allocate against a line that
    already carries partial matches."""
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="500.00")
    contact_id = str(await _get_contact_id())

    async with AsyncSessionLocal() as session:
        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
        assert bsl is not None
        bsl.status = StatementLineStatus.PARTIAL
        await session.commit()

    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "payment",
            "payment": {"contact_id": contact_id},
        },
    )
    assert r.status_code == 422, r.text

    async with AsyncSessionLocal() as session:
        matches = (
            (await session.execute(select(BslMatch).where(BslMatch.bsl_id == uuid.UUID(bsl_id))))
            .scalars()
            .all()
        )
        assert matches == []


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


async def test_create_and_match_amount_mismatch_rolls_back_fully(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """Expense line totals that don't reconcile with the BSL amount are
    rejected BEFORE anything commits — genuinely atomic rollback, no orphan
    expense row survives.
    """
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="-150.00")
    expense_account_id = str(await _get_expense_account_id())

    unique_ref = f"MISMATCH-{uuid.uuid4().hex[:8].upper()}"
    r = await api_client.post(
        "/api/v1/reconciliation/create_and_match",
        json={
            "bsl_id": bsl_id,
            "record_type": "expense",
            "expense": {
                "reference": unique_ref,
                "lines": [
                    {
                        "description": "Wrong amount",
                        "account_id": expense_account_id,
                        "quantity": "1",
                        "unit_price": "1.00",  # does NOT reconcile with 150.00
                    }
                ],
            },
        },
    )
    assert r.status_code == 422, r.text

    # Nothing persisted: no expense row with this reference, BSL unchanged.
    async with AsyncSessionLocal() as session:
        orphan = (
            await session.execute(
                select(Expense).where(Expense.reference == unique_ref)
            )
        ).scalars().first()
        assert orphan is None, "amount-mismatch rollback left an orphan expense row"

        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
        assert bsl.status == StatementLineStatus.UNMATCHED
        assert bsl.matched_entry_id is None


async def test_create_and_match_post_step_failure_leaves_draft_and_no_match(
    api_client: AsyncClient, bank_account_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force ``api_post_expense`` (the POST phase, DISTINCT from both the
    amount-mismatch pre-flight and the final match-step) to fail. The DRAFT
    expense created moments earlier by ``api_create`` (already committed —
    see the atomicity docstring on ``create_and_match``) survives untouched;
    no journal entry, no bsl_matches row, BSL stays UNMATCHED. Never a
    partial match.
    """
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="-150.00")
    expense_account_id = str(await _get_expense_account_id())
    company_id = await _get_company_id()

    unique_ref = f"POSTFAIL-{uuid.uuid4().hex[:8].upper()}"

    async def flaky_post_expense(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected post-step failure")

    monkeypatch.setattr(expenses_svc_module, "api_post_expense", flaky_post_expense)

    async with AsyncSessionLocal() as session:
        with pytest.raises(RuntimeError, match="injected post-step failure"):
            await svc.create_and_match(
                session,
                uuid.UUID(bsl_id),
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                actor="test",
                record_type="expense",
                expense_spec={
                    "reference": unique_ref,
                    "lines": [
                        {
                            "description": "Office supplies",
                            "account_id": uuid.UUID(expense_account_id),
                            "quantity": Decimal("1"),
                            "unit_price": Decimal("150.00"),
                        }
                    ],
                },
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        # The DRAFT expense survives — it committed before the post call
        # was ever attempted.
        draft = (
            await session.execute(
                select(Expense).where(Expense.reference == unique_ref)
            )
        ).scalars().first()
        assert draft is not None
        assert draft.status == ExpenseStatus.DRAFT
        assert draft.journal_entry_id is None

        # No match, no journal entry, BSL unchanged.
        remaining = (
            await session.execute(
                select(BslMatch).where(BslMatch.bsl_id == uuid.UUID(bsl_id))
            )
        ).scalars().all()
        assert remaining == [], "a partial/orphan bsl_matches row was persisted"

        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
        assert bsl.status == StatementLineStatus.UNMATCHED
        assert bsl.matched_entry_id is None


async def test_create_and_match_match_step_failure_leaves_no_partial_match(
    api_client: AsyncClient, bank_account_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the final add_match to fail. Per the documented atomicity
    residual: the posted record survives (this slice does not achieve
    single-transaction atomicity across create+post+match — see
    services/reconciliation.create_and_match docstring), but the invariant
    that MUST hold is upheld: no bsl_matches row and the BSL stays
    UNMATCHED. Never a corrupt or partial match.

    Calls the service function directly (not through the HTTP router) —
    httpx's ASGITransport re-raises unhandled exceptions into the test by
    default (``raise_app_exceptions=True``) rather than returning a 500,
    so asserting the rollback invariant is cleanest at the service layer,
    matching the ``test_intercompany.py`` failure-injection convention.
    """
    bsl_id = await _create_bsl(api_client, bank_account_id, amount="500.00")
    contact_id = await _get_contact_id()
    company_id = await _get_company_id()

    unique_ref = f"MATCHFAIL-{uuid.uuid4().hex[:8].upper()}"

    async def flaky_add_match(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected match-step failure")

    monkeypatch.setattr(svc, "add_match", flaky_add_match)

    async with AsyncSessionLocal() as session:
        with pytest.raises(RuntimeError, match="injected match-step failure"):
            await svc.create_and_match(
                session,
                uuid.UUID(bsl_id),
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                actor="test",
                record_type="payment",
                payment_spec={"contact_id": contact_id, "reference": unique_ref},
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        remaining = (
            await session.execute(
                select(BslMatch).where(BslMatch.bsl_id == uuid.UUID(bsl_id))
            )
        ).scalars().all()
        assert remaining == [], "a partial/orphan bsl_matches row was persisted"

        bsl = await session.get(BankStatementLine, uuid.UUID(bsl_id))
        assert bsl.status == StatementLineStatus.UNMATCHED
        assert bsl.matched_entry_id is None

        # The payment itself DOES survive (posted, orphaned from the
        # match) — the documented residual, not a bug. Select it by its
        # unique reference: test_payments*.py leave DRAFT 500.00 payments
        # on the same shared contact, so a (company, contact, amount)
        # predicate is ambiguous in full-suite order.
        orphan_payment = (
            await session.execute(
                select(Payment).where(Payment.reference == unique_ref)
            )
        ).scalars().first()
        assert orphan_payment is not None
        assert orphan_payment.company_id == company_id
        assert orphan_payment.contact_id == contact_id
        assert orphan_payment.amount == Decimal("500.00")
        assert orphan_payment.status.value == "POSTED"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_create_and_match_tenant_isolation(
    api_client: AsyncClient, bank_account_id: str
) -> None:
    """A BSL belonging to a different company 404s from create_and_match."""
    foreign_company_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        account = (
            await session.execute(
                select(Account).where(Account.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        assert account is not None

        second_company = Company(
            id=foreign_company_id,
            name="Create-And-Match Isolation Co",
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
            txn_date=__import__("datetime").date(2026, 5, 1),
            amount=Decimal("100.00"),
            status=StatementLineStatus.UNMATCHED,
        )
        session.add(foreign_bsl)
        await session.commit()
        foreign_bsl_id = str(foreign_bsl.id)

    try:
        r = await api_client.post(
            "/api/v1/reconciliation/create_and_match",
            json={
                "bsl_id": foreign_bsl_id,
                "record_type": "payment",
                "payment": {"contact_id": str(uuid.uuid4())},
            },
        )
        assert r.status_code == 404
    finally:
        async with AsyncSessionLocal() as session:
            bsl_obj = await session.get(BankStatementLine, uuid.UUID(foreign_bsl_id))
            if bsl_obj is not None:
                await session.delete(bsl_obj)
                await session.flush()
            co = await session.get(Company, foreign_company_id)
            if co is not None:
                await session.delete(co)
            await session.commit()
