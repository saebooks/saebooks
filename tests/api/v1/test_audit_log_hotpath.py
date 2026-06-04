"""C2 — audit_log hot-path coverage (TDD).

Asserts that the 8 compliance-relevant hot-path events each write exactly
one attributable ``audit_log`` row IN THE SAME TRANSACTION as the action:

  1. invoice DRAFT->POSTED   action="invoice.post"
  2. invoice POSTED->VOIDED  action="invoice.void"
  3. bill    DRAFT->POSTED   action="bill.post"
  4. bill    POSTED->VOIDED  action="bill.void"
  5. payment DRAFT->POSTED   action="payment.post"
  6. payment POSTED->VOIDED  action="payment.void"
  7. credit  DRAFT->POSTED   action="credit_note.post"
  8. JE post w/ period-lock override   action="journal.override_post"

Every row must carry ``actor_user_id`` == the acting user UUID (NOT the
JWT prefix), the right ``table_name`` / ``row_id`` / non-empty
``row_snapshot``, and (for void/override) the ``reason``.

In-transaction guarantee: if the action ROLLS BACK (rejected period-lock
override), NO orphan audit row persists.

Admin read endpoint: GET /api/v1/audit-log is admin-gated and returns the
real audit_log rows; non-admin is denied.

Events 1-7 run against the seeded default company (which carries the full
CoA including the 1-1200 AR / AP control accounts the post pipeline needs).
The JE-override events use an isolated company with a period lock.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio

os.environ.setdefault("SAEBOOKS_ENV", "test")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.audit_log import AuditLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.user import User
from saebooks.services import journal as journal_svc
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Real-user + JWT helpers
# ---------------------------------------------------------------------------


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {"sub": str(user.id), "role": user.role, "tenant_id": str(user.tenant_id)}
    )


async def _make_user(role: str = "admin") -> User:
    user = User(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        username=f"audit-{role}-{uuid.uuid4().hex[:8]}",
        email=f"audit-{role}-{uuid.uuid4().hex[:8]}@test.invalid",
        role=role,
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _drop_user(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.id == user_id))
        await session.commit()


@pytest_asyncio.fixture
async def acting_user() -> AsyncIterator[User]:
    user = await _make_user("admin")
    try:
        yield user
    finally:
        await _drop_user(user.id)


def _client(user: User, company_id: uuid.UUID | None = None) -> AsyncClient:
    headers = {"Authorization": f"Bearer {_mint(user)}"}
    if company_id is not None:
        headers["X-Company-Id"] = str(company_id)
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    )


async def _audit_rows(table_name: str, row_id: str) -> list[AuditLog]:
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(_TENANT)
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.table_name == table_name,
                    AuditLog.row_id == str(row_id),
                ).order_by(AuditLog.at.asc())
            )
        ).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# Seeded-default-company dependency lookups
# ---------------------------------------------------------------------------


async def _seeded_deps() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        income = (await session.execute(
            select(Account).where(
                Account.archived_at.is_(None),
                Account.account_type == AccountType.INCOME,
                Account.is_header.is_(False),
                Account.tenant_id == _TENANT,
            ).limit(1)
        )).scalars().first()
        expense = (await session.execute(
            select(Account).where(
                Account.archived_at.is_(None),
                Account.account_type == AccountType.EXPENSE,
                Account.is_header.is_(False),
                Account.tenant_id == _TENANT,
            ).limit(1)
        )).scalars().first()
        bank = (await session.execute(
            select(Account).where(
                Account.archived_at.is_(None),
                Account.account_type == AccountType.ASSET,
                Account.is_header.is_(False),
                Account.tenant_id == _TENANT,
            ).limit(1)
        )).scalars().first()
        contact = (await session.execute(
            select(Contact).where(
                Contact.archived_at.is_(None),
                Contact.tenant_id == _TENANT,
            ).limit(1)
        )).scalars().first()
    assert income and expense and bank and contact, "seeded default company incomplete"
    return {
        "income_account_id": str(income.id),
        "expense_account_id": str(expense.id),
        "bank_account_id": str(bank.id),
        "contact_id": str(contact.id),
    }


@pytest_asyncio.fixture
async def deps() -> dict[str, str]:
    return await _seeded_deps()


def _inv_payload(d: dict, issue_date: str = "2026-04-01") -> dict:
    return {
        "contact_id": d["contact_id"], "issue_date": issue_date, "due_date": "2026-05-01",
        "lines": [{"description": "Audit consulting", "account_id": d["income_account_id"],
                   "quantity": "1", "unit_price": "500.00", "discount_pct": "0"}],
    }


def _bill_payload(d: dict, issue_date: str = "2026-04-01") -> dict:
    return {
        "contact_id": d["contact_id"], "issue_date": issue_date, "due_date": "2026-05-01",
        "lines": [{"description": "Audit supplies", "account_id": d["expense_account_id"],
                   "quantity": "1", "unit_price": "200.00", "discount_pct": "0"}],
    }


def _payment_payload(d: dict) -> dict:
    return {
        "contact_id": d["contact_id"], "bank_account_id": d["bank_account_id"],
        "payment_date": "2026-04-15", "amount": "300.00", "direction": "INCOMING",
        "method": "eft", "reference": "AUDIT-PAY-001",
    }


def _cn_payload(d: dict) -> dict:
    return {
        "contact_id": d["contact_id"], "issue_date": "2026-04-01",
        "lines": [{"description": "Audit credit", "account_id": d["income_account_id"],
                   "quantity": "1", "unit_price": "100.00", "discount_pct": "0"}],
    }


# ===========================================================================
# Event 1 — invoice DRAFT -> POSTED
# ===========================================================================


async def test_invoice_post_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        r = await client.post("/api/v1/invoices", json=_inv_payload(deps))
        assert r.status_code == 201, r.text
        inv = r.json()
        r = await client.post(f"/api/v1/invoices/{inv['id']}/post",
                              headers={"If-Match": str(inv["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("invoices", inv["id"])
    posts = [x for x in rows if x.action == "invoice.post"]
    assert len(posts) == 1, f"expected one invoice.post row, got {[x.action for x in rows]}"
    row = posts[0]
    assert row.actor_user_id == acting_user.id, "actor must be user UUID, not JWT prefix"
    assert row.table_name == "invoices"
    assert row.row_id == inv["id"]
    assert row.row_snapshot and row.row_snapshot.get("status") == "POSTED"
    assert row.reason is None


# ===========================================================================
# Event 2 — invoice POSTED -> VOIDED
# ===========================================================================


async def test_invoice_void_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        inv = (await client.post("/api/v1/invoices", json=_inv_payload(deps))).json()
        posted = (await client.post(f"/api/v1/invoices/{inv['id']}/post",
                                   headers={"If-Match": str(inv["version"])})).json()
        r = await client.post(f"/api/v1/invoices/{inv['id']}/void",
                             headers={"If-Match": str(posted["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("invoices", inv["id"])
    voids = [x for x in rows if x.action == "invoice.void"]
    assert len(voids) == 1, f"expected one invoice.void row, got {[x.action for x in rows]}"
    assert voids[0].actor_user_id == acting_user.id
    assert voids[0].row_snapshot.get("status") == "VOIDED"


# ===========================================================================
# Event 3 — bill DRAFT -> POSTED
# ===========================================================================


async def test_bill_post_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        bill = (await client.post("/api/v1/bills", json=_bill_payload(deps))).json()
        r = await client.post(f"/api/v1/bills/{bill['id']}/post",
                             headers={"If-Match": str(bill["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("bills", bill["id"])
    posts = [x for x in rows if x.action == "bill.post"]
    assert len(posts) == 1, f"expected one bill.post row, got {[x.action for x in rows]}"
    assert posts[0].actor_user_id == acting_user.id
    assert posts[0].row_snapshot.get("status") == "POSTED"


# ===========================================================================
# Event 4 — bill POSTED -> VOIDED
# ===========================================================================


async def test_bill_void_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        bill = (await client.post("/api/v1/bills", json=_bill_payload(deps))).json()
        posted = (await client.post(f"/api/v1/bills/{bill['id']}/post",
                                   headers={"If-Match": str(bill["version"])})).json()
        r = await client.post(f"/api/v1/bills/{bill['id']}/void",
                             headers={"If-Match": str(posted["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("bills", bill["id"])
    voids = [x for x in rows if x.action == "bill.void"]
    assert len(voids) == 1, f"expected one bill.void row, got {[x.action for x in rows]}"
    assert voids[0].actor_user_id == acting_user.id
    assert voids[0].row_snapshot.get("status") == "VOIDED"


# ===========================================================================
# Event 5 — payment DRAFT -> POSTED
# ===========================================================================


async def test_payment_post_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        pay = (await client.post("/api/v1/payments", json=_payment_payload(deps))).json()
        r = await client.post(f"/api/v1/payments/{pay['id']}/post",
                             headers={"If-Match": str(pay["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("payments", pay["id"])
    posts = [x for x in rows if x.action == "payment.post"]
    assert len(posts) == 1, f"expected one payment.post row, got {[x.action for x in rows]}"
    assert posts[0].actor_user_id == acting_user.id
    assert posts[0].row_snapshot


# ===========================================================================
# Event 6 — payment POSTED -> VOIDED
# ===========================================================================


async def test_payment_void_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        pay = (await client.post("/api/v1/payments", json=_payment_payload(deps))).json()
        posted = (await client.post(f"/api/v1/payments/{pay['id']}/post",
                                   headers={"If-Match": str(pay["version"])})).json()
        r = await client.delete(f"/api/v1/payments/{pay['id']}",
                               headers={"If-Match": str(posted["version"])})
        assert r.status_code == 204, r.text
    rows = await _audit_rows("payments", pay["id"])
    voids = [x for x in rows if x.action == "payment.void"]
    assert len(voids) == 1, f"expected one payment.void row, got {[x.action for x in rows]}"
    assert voids[0].actor_user_id == acting_user.id
    assert voids[0].row_snapshot.get("status") == "VOIDED"


# ===========================================================================
# Event 7 — credit-note DRAFT -> POSTED
# ===========================================================================


async def test_credit_note_post_writes_attributable_audit_row(acting_user, deps):
    async with _client(acting_user) as client:
        cn = (await client.post("/api/v1/credit_notes", json=_cn_payload(deps))).json()
        r = await client.post(f"/api/v1/credit_notes/{cn['id']}/post",
                             headers={"If-Match": str(cn["version"])})
        assert r.status_code == 200, r.text
    rows = await _audit_rows("credit_notes", cn["id"])
    posts = [x for x in rows if x.action == "credit_note.post"]
    assert len(posts) == 1, f"expected one credit_note.post row, got {[x.action for x in rows]}"
    assert posts[0].actor_user_id == acting_user.id
    assert posts[0].row_snapshot.get("status") == "POSTED"


# ---------------------------------------------------------------------------
# Isolated locked company for JE override tests
# ---------------------------------------------------------------------------


async def _locked_company() -> dict:
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(_TENANT)
        session.add(Company(id=cid, tenant_id=_TENANT, name=f"AUDIT Lock {cid.hex[:6]}"))
        await session.flush()
        expense = Account(company_id=cid, tenant_id=_TENANT, code=f"6-{cid.hex[:4]}",
                          name="Audit Expense", account_type=AccountType.EXPENSE, is_header=False)
        bank = Account(company_id=cid, tenant_id=_TENANT, code=f"1-{cid.hex[:4]}",
                       name="Audit Bank", account_type=AccountType.ASSET, is_header=False)
        session.add_all([expense, bank])
        await session.flush()
        await journal_svc.lock_period(session, cid, date(2026, 3, 31), locked_by="audit-test")
        await session.commit()
        for o in (expense, bank):
            await session.refresh(o)
        return {"company_id": cid, "expense_account_id": expense.id, "bank_account_id": bank.id}


def _je_payload(ctx: dict) -> dict:
    return {
        "entry_date": "2026-01-15",
        "narration": "audit override",
        "lines": [
            {"account_id": str(ctx["expense_account_id"]), "debit": "100.00", "credit": "0"},
            {"account_id": str(ctx["bank_account_id"]), "debit": "0", "credit": "100.00"},
        ],
    }


# ===========================================================================
# In-transaction guarantee — rejected period-lock override writes NO row
# ===========================================================================


async def test_rejected_override_leaves_no_orphan_audit_row(acting_user):
    ctx = await _locked_company()
    async with _client(acting_user, ctx["company_id"]) as client:
        je = (await client.post("/api/v1/journal_entries", json=_je_payload(ctx))).json()
        # Post with NO override reason -> rejected (422), period locked.
        r = await client.post(f"/api/v1/journal_entries/{je['id']}/post",
                             headers={"If-Match": str(je["version"])})
        assert r.status_code == 422, f"expected 422 rejected override, got {r.status_code}: {r.text}"
    rows = await _audit_rows("journal_entries", je["id"])
    assert rows == [], f"rejected override must leave NO audit row, found {[x.action for x in rows]}"


# ===========================================================================
# Event 8 — JE post WITH accepted period-lock override
# ===========================================================================


async def test_je_override_post_writes_audit_row_with_reason(acting_user):
    ctx = await _locked_company()
    override_reason = "Auditor-approved prior-period adjustment per engagement letter"
    async with _client(acting_user, ctx["company_id"]) as client:
        je = (await client.post("/api/v1/journal_entries", json=_je_payload(ctx))).json()
        r = await client.post(f"/api/v1/journal_entries/{je['id']}/post",
                             headers={"If-Match": str(je["version"])},
                             json={"override_reason": override_reason})
        assert r.status_code == 200, f"override post should succeed: {r.text}"
    rows = await _audit_rows("journal_entries", je["id"])
    override_rows = [x for x in rows if x.action == "journal.override_post"]
    assert len(override_rows) == 1, f"expected one journal.override_post row, got {[x.action for x in rows]}"
    row = override_rows[0]
    assert row.actor_user_id == acting_user.id
    assert row.reason == override_reason
    assert row.row_snapshot


# ===========================================================================
# Admin read endpoint — GET /api/v1/audit-log
# ===========================================================================


async def test_admin_audit_log_endpoint_returns_rows(acting_user, deps):
    async with _client(acting_user) as client:
        inv = (await client.post("/api/v1/invoices", json=_inv_payload(deps))).json()
        r = await client.post(f"/api/v1/invoices/{inv['id']}/post",
                             headers={"If-Match": str(inv["version"])})
        assert r.status_code == 200, r.text
        r = await client.get("/api/v1/audit-log", params={"row_id": inv["id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"] if isinstance(body, dict) else body
        actions = [it["action"] for it in items]
        assert "invoice.post" in actions, f"admin reader should surface invoice.post, got {actions}"
        r = await client.get("/api/v1/audit-log",
                            params={"table": "invoices", "row_id": inv["id"]})
        assert r.status_code == 200, r.text


async def test_admin_audit_log_endpoint_denies_non_admin():
    viewer = await _make_user("viewer")
    try:
        async with _client(viewer) as client:
            r = await client.get("/api/v1/audit-log")
            assert r.status_code == 403, f"viewer must be denied, got {r.status_code}"
    finally:
        await _drop_user(viewer.id)
