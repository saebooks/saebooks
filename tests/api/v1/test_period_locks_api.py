"""M3b — period-locks CRUD (``/api/v1/period-close/locks``).

Covers the gap flagged in the module audit: ``PeriodLock`` rows can only be
created as a side effect of ``close-year``. These three admin-only routes
let an operator lock/list/unlock a period independently:

  * ``POST   /api/v1/period-close/locks``            — create a lock; 409
    if it does not extend beyond the company's current lock.
  * ``GET    /api/v1/period-close/locks``             — list history +
    ``effective_locked_through``.
  * ``DELETE /api/v1/period-close/locks/{lock_id}``   — remove a lock;
    requires ``?reason=``; writes an ``audit_log`` forensic row; posting
    enforcement recedes to whatever locks remain.

Real posting enforcement (``services.journal._check_period_lock``) is
exercised end-to-end via ``POST /api/v1/journal_entries/{id}/post`` rather
than mocked, so these tests prove the lock rows this API creates/removes
actually gate posting.

Uses the same real-user + JWT admin/non-admin pattern as
``test_audit_log_hotpath.py`` (these routes are ``_require_admin``-gated,
so the static dev-bearer + ``X-Admin`` header shortcut doesn't exercise the
non-admin-rejection path).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

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
from saebooks.models.journal import JournalEntry, PeriodLock
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Real-user + JWT helpers (mirrors test_audit_log_hotpath.py)
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
        username=f"lock-{role}-{uuid.uuid4().hex[:8]}",
        email=f"lock-{role}-{uuid.uuid4().hex[:8]}@test.invalid",
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
async def admin_user() -> AsyncIterator[User]:
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


# ---------------------------------------------------------------------------
# Isolated company + accounts (no pre-existing lock — routes create it)
# ---------------------------------------------------------------------------


async def _new_company() -> dict:
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(_TENANT)
        session.add(Company(id=cid, tenant_id=_TENANT, name=f"LOCK Test {cid.hex[:6]}"))
        await session.flush()
        expense = Account(company_id=cid, tenant_id=_TENANT, code=f"6-{cid.hex[:4]}",
                          name="Lock Expense", account_type=AccountType.EXPENSE, is_header=False)
        bank = Account(company_id=cid, tenant_id=_TENANT, code=f"1-{cid.hex[:4]}",
                       name="Lock Bank", account_type=AccountType.ASSET, is_header=False)
        session.add_all([expense, bank])
        await session.commit()
        for o in (expense, bank):
            await session.refresh(o)
        return {"company_id": cid, "expense_account_id": expense.id, "bank_account_id": bank.id}


def _je_payload(ctx: dict, entry_date: str) -> dict:
    return {
        "entry_date": entry_date,
        "narration": "period-lock test entry",
        "lines": [
            {"account_id": str(ctx["expense_account_id"]), "debit": "50.00", "credit": "0"},
            {"account_id": str(ctx["bank_account_id"]), "debit": "0", "credit": "50.00"},
        ],
    }


async def _audit_rows_for_lock(lock_id: str) -> list[AuditLog]:
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(_TENANT)
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.table_name == "period_locks",
                    AuditLog.row_id == lock_id,
                )
            )
        ).scalars().all()
    return list(rows)


async def _drop_locks(*lock_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(PeriodLock).where(PeriodLock.id.in_(lock_ids)))
        await session.commit()


async def _drop_companies(*company_ids: uuid.UUID) -> None:
    """Teardown for ``_new_company`` — leaves the default tenant as it found
    it.

    ``journal_lines.account_id`` is ON DELETE RESTRICT, so a posted JE in
    one of these companies (e.g.
    ``test_delete_lock_removes_row_audits_and_enforcement_recedes`` posts one
    once its blocking lock is removed) would block the company->accounts
    cascade. Delete the entries first (their lines cascade via
    ``journal_lines.entry_id`` ON DELETE CASCADE); ``period_locks`` and
    ``accounts`` both cascade off ``company_id`` ON DELETE CASCADE, so the
    company delete cleans those up (and any locks a test didn't already
    remove via ``_drop_locks``) on its own. Mirrors
    ``tests/conftest.py::seeded_company``'s teardown.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_delete(JournalEntry).where(JournalEntry.company_id.in_(company_ids))
        )
        for cid in company_ids:
            co = await session.get(Company, cid)
            if co is not None:
                await session.delete(co)
        await session.commit()


@pytest_asyncio.fixture
async def new_company() -> AsyncIterator[Callable[[], Awaitable[dict]]]:
    """Factory fixture wrapping ``_new_company`` with teardown.

    Returns an async factory (rather than a single pre-built ctx) since
    several tests here create more than one isolated company per test
    (e.g. ``test_delete_lock_cross_company_returns_404``'s ``ctx_a``/
    ``ctx_b``); every company the factory hands out is torn down at the
    end of the test regardless of how many were created.
    """
    created: list[uuid.UUID] = []

    async def _make() -> dict:
        ctx = await _new_company()
        created.append(ctx["company_id"])
        return ctx

    try:
        yield _make
    finally:
        if created:
            await _drop_companies(*created)


# ===========================================================================
# POST /period-close/locks — create + 409 no-extend guard
# ===========================================================================


async def test_create_lock_then_blocks_posting_into_it(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        r = await client.post(
            "/api/v1/period-close/locks",
            json={"locked_through": "2024-03-31", "reason": "Q1 completed"},
        )
        assert r.status_code == 201, r.text
        lock = r.json()
        assert lock["locked_through"] == "2024-03-31"
        assert lock["reason"] == "Q1 completed"

        je = (await client.post("/api/v1/journal_entries",
                                json=_je_payload(ctx, "2024-02-15"))).json()
        r = await client.post(f"/api/v1/journal_entries/{je['id']}/post",
                             headers={"If-Match": str(je["version"])})
        assert r.status_code == 422, f"expected 422 lock rejection, got {r.status_code}: {r.text}"
        assert "locked" in r.text.lower(), r.text


async def test_create_lock_not_extending_current_max_returns_409(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        r = await client.post(
            "/api/v1/period-close/locks",
            json={"locked_through": "2024-06-30"},
        )
        assert r.status_code == 201, r.text

        # Equal — does not extend beyond current max.
        r = await client.post(
            "/api/v1/period-close/locks",
            json={"locked_through": "2024-06-30"},
        )
        assert r.status_code == 409, r.text

        # Earlier — also does not extend.
        r = await client.post(
            "/api/v1/period-close/locks",
            json={"locked_through": "2024-01-31"},
        )
        assert r.status_code == 409, r.text


# ===========================================================================
# GET /period-close/locks — list + effective_locked_through
# ===========================================================================


async def test_list_locks_returns_history_and_effective_max(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        r = await client.get("/api/v1/period-close/locks")
        assert r.status_code == 200, r.text
        assert r.json() == {"items": [], "effective_locked_through": None}

        await client.post("/api/v1/period-close/locks",
                         json={"locked_through": "2024-03-31"})
        await client.post("/api/v1/period-close/locks",
                         json={"locked_through": "2024-06-30"})

        r = await client.get("/api/v1/period-close/locks")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["effective_locked_through"] == "2024-06-30"
        assert len(body["items"]) == 2
        dates = {item["locked_through"] for item in body["items"]}
        assert dates == {"2024-03-31", "2024-06-30"}


# ===========================================================================
# DELETE /period-close/locks/{id} — reason required, forensic audit, recede
# ===========================================================================


async def test_delete_lock_requires_nonblank_reason(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        lock = (await client.post(
            "/api/v1/period-close/locks", json={"locked_through": "2024-03-31"},
        )).json()

        r = await client.delete(f"/api/v1/period-close/locks/{lock['id']}")
        assert r.status_code == 422, r.text  # missing entirely

        r = await client.delete(f"/api/v1/period-close/locks/{lock['id']}",
                               params={"reason": "   "})
        assert r.status_code == 422, r.text  # blank after strip


async def test_delete_lock_removes_row_audits_and_enforcement_recedes(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        lock1 = (await client.post(
            "/api/v1/period-close/locks", json={"locked_through": "2024-03-31"},
        )).json()
        lock2 = (await client.post(
            "/api/v1/period-close/locks", json={"locked_through": "2024-06-30"},
        )).json()

        # entry_date inside Q2 (after lock1, inside lock2) — blocked while
        # lock2 exists.
        je = (await client.post("/api/v1/journal_entries",
                                json=_je_payload(ctx, "2024-05-15"))).json()
        r = await client.post(f"/api/v1/journal_entries/{je['id']}/post",
                             headers={"If-Match": str(je["version"])})
        assert r.status_code == 422, r.text

        # Remove lock2 with a forensic reason.
        delete_reason = "Q2 reopened for late supplier invoice per CFO approval"
        r = await client.delete(f"/api/v1/period-close/locks/{lock2['id']}",
                               params={"reason": delete_reason})
        assert r.status_code == 204, r.text

        # Row is gone from the list.
        r = await client.get("/api/v1/period-close/locks")
        ids = {item["id"] for item in r.json()["items"]}
        assert lock2["id"] not in ids
        assert lock1["id"] in ids
        assert r.json()["effective_locked_through"] == "2024-03-31"

        # Forensic audit_log row was written.
        rows = await _audit_rows_for_lock(lock2["id"])
        deletes = [row for row in rows if row.action == "period_lock.delete"]
        assert len(deletes) == 1, f"expected one period_lock.delete row, got {[r.action for r in rows]}"
        assert deletes[0].actor_user_id == admin_user.id
        assert deletes[0].reason == delete_reason
        assert deletes[0].row_snapshot.get("locked_through") == "2024-06-30"

        # Enforcement receded — same JE now posts (2024-05-15 > remaining
        # max 2024-03-31).
        r = await client.post(f"/api/v1/journal_entries/{je['id']}/post",
                             headers={"If-Match": str(je["version"])})
        assert r.status_code == 200, f"posting should now succeed: {r.text}"

        await _drop_locks(uuid.UUID(lock1["id"]))


async def test_delete_lock_cross_company_returns_404(admin_user, new_company):
    ctx_a = await new_company()
    ctx_b = await new_company()
    async with _client(admin_user, ctx_a["company_id"]) as client_a:
        lock = (await client_a.post(
            "/api/v1/period-close/locks", json={"locked_through": "2024-03-31"},
        )).json()

    async with _client(admin_user, ctx_b["company_id"]) as client_b:
        r = await client_b.delete(f"/api/v1/period-close/locks/{lock['id']}",
                                 params={"reason": "wrong company probe"})
        assert r.status_code == 404, r.text

    await _drop_locks(uuid.UUID(lock["id"]))


async def test_delete_lock_not_found_returns_404(admin_user, new_company):
    ctx = await new_company()
    async with _client(admin_user, ctx["company_id"]) as client:
        r = await client.delete(f"/api/v1/period-close/locks/{uuid.uuid4()}",
                               params={"reason": "no such lock"})
        assert r.status_code == 404, r.text


# ===========================================================================
# Non-admin rejection on all three routes
# ===========================================================================


async def test_locks_routes_deny_non_admin():
    viewer = await _make_user("viewer")
    try:
        async with _client(viewer) as client:
            r = await client.post("/api/v1/period-close/locks",
                                 json={"locked_through": "2024-03-31"})
            assert r.status_code == 403, r.text

            r = await client.get("/api/v1/period-close/locks")
            assert r.status_code == 403, r.text

            r = await client.delete(f"/api/v1/period-close/locks/{uuid.uuid4()}",
                                   params={"reason": "probe"})
            assert r.status_code == 403, r.text
    finally:
        await _drop_user(viewer.id)


# ===========================================================================
# Tenant-B isolation — a real second tenant, not just a second company
# under the SAME tenant (test_delete_lock_cross_company_returns_404 above
# only covers the latter)
# ===========================================================================


@pytest_asyncio.fixture
async def tenant_b_company_id() -> AsyncIterator[str]:
    """A real second tenant + its own Company, for a genuine cross-tenant
    probe. Mirrors saebooks-m3's ``tenant_b`` fixture
    (tests/api/v1/test_reports_csv.py, commit 6b1bfd3) — a bare random
    company id with no Company row would always 404 at
    ``get_active_company_id`` regardless of whether tenant scoping
    actually works, making the isolation assertion dead code. A real
    tenant + company makes the probe exercise the real predicate.
    """
    suffix = uuid.uuid4().hex[:8]
    tenant_id = uuid.uuid4()
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(id=tenant_id, name=f"LockTenantB-{suffix}", slug=f"lock-tenant-b-{suffix}")
        )
        await session.flush()
        session.add(Company(id=company_id, tenant_id=tenant_id, name=f"LockTenantB-{suffix}"))
        await session.commit()
    try:
        yield str(company_id)
    finally:
        async with AsyncSessionLocal() as session:
            company_row = await session.get(Company, company_id)
            if company_row is not None:
                await session.delete(company_row)
            tenant_row = await session.get(Tenant, tenant_id)
            if tenant_row is not None:
                await session.delete(tenant_row)
            await session.commit()


async def test_locks_routes_tenant_b_company_id_returns_404(admin_user, tenant_b_company_id):
    """Tenant A's token + tenant B's company id (X-Company-Id) must 404 on
    list/create/delete — ``get_active_company_id`` requires the header's
    company to belong to the caller's own tenant.
    """
    async with _client(admin_user, uuid.UUID(tenant_b_company_id)) as client:
        r = await client.get("/api/v1/period-close/locks")
        assert r.status_code == 404, r.text

        r = await client.post("/api/v1/period-close/locks",
                             json={"locked_through": "2024-03-31"})
        assert r.status_code == 404, r.text

        r = await client.delete(f"/api/v1/period-close/locks/{uuid.uuid4()}",
                               params={"reason": "cross-tenant probe"})
        assert r.status_code == 404, r.text
