"""API contract tests for /api/v1/statements (Gitea #28, Phase 1).

Covers:
* POST /ingest returns 201 + full detail shape (mocked ingest_statement)
* GET /statements returns items + total
* GET /statements/{id} returns detail with lines
* GET /statements/{unknown_id} → 404
* Read-only API token rejected on POST (403)
* RLS: statement created under tenant A is invisible (404/absent) to tenant B

The suite mocks ``ingest_statement`` so it does not hit Paperless or the LLM.
All tests that need a real DB are marked ``postgres_only``.
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select as sa_select
from sqlalchemy import text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.supplier_statement import (
    StatementMatchStatus,
    StatementStatus,
    SupplierStatement,
    SupplierStatementLine,
)
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _default_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                sa_select(Company).where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if company is None:
            pytest.skip("No active company in default tenant")
        return company.id


async def _seed_statement(
    *,
    stmt_id: uuid.UUID,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
    company_id: uuid.UUID,
    source_document_id: int = 42,
    status: str = StatementStatus.RECONCILED.value,
    lines: bool = False,
) -> None:
    """Insert a SupplierStatement (and optional line) via the owner session."""
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_id)
        stmt = SupplierStatement(
            id=stmt_id,
            tenant_id=tenant_id,
            company_id=company_id,
            source_document_id=source_document_id,
            supplier_name="Acme Supplies Pty Ltd",
            supplier_abn="12 345 678 901",
            customer_ref="CUST-001",
            statement_date=date(2026, 5, 31),
            terms="Net 30",
            opening_balance=Decimal("0.00"),
            closing_balance=Decimal("1100.00"),
            currency="AUD",
            status=status,
            our_ap_as_at=Decimal("1100.00"),
            balance_delta=Decimal("0.00"),
            extraction_meta={"model_used": "gpt-4o-mini", "escalated": False},
        )
        session.add(stmt)
        if lines:
            await session.flush()
            line = SupplierStatementLine(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                statement_id=stmt_id,
                line_date=date(2026, 5, 1),
                line_type="invoice",
                reference="INV-001",
                description="Office supplies",
                amount=Decimal("1100.00"),
                match_status=StatementMatchStatus.MATCHED.value,
            )
            session.add(line)
        await session.commit()


async def _delete_statement(stmt_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM supplier_statement_lines WHERE statement_id = :id").bindparams(id=stmt_id)
        )
        await session.execute(
            text("DELETE FROM supplier_statements WHERE id = :id").bindparams(id=stmt_id)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# POST /statements/ingest — returns 201 + detail shape
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_ingest_returns_201_with_detail(api_client: AsyncClient) -> None:
    """POST /ingest returns 201 with the full detail shape.

    Seeds a real SupplierStatement so that session.get() inside the handler
    succeeds after commit. Mocks ingest_statement to return that row, skipping
    Paperless + LLM.
    """
    company_id = await _default_company_id()
    stmt_id = uuid.uuid4()
    await _seed_statement(stmt_id=stmt_id, company_id=company_id, source_document_id=42, lines=True)

    try:
        # Load the seeded row to give back from the mock.
        async with AsyncSessionLocal() as session:
            from sqlalchemy.orm import selectinload
            stmt_row = await session.get(
                SupplierStatement, stmt_id,
                options=[selectinload(SupplierStatement.lines)],
            )
            assert stmt_row is not None

        with patch(
            "saebooks.api.v1.statements.ingest_statement",
            new_callable=AsyncMock,
            return_value=stmt_row,
        ):
            r = await api_client.post(
                "/api/v1/statements/ingest",
                json={"paperless_document_id": 42},
            )
    finally:
        await _delete_statement(stmt_id)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_document_id"] == 42
    assert "status" in body
    assert "lines" in body
    assert isinstance(body["lines"], list)
    assert "supplier_name" in body
    assert "closing_balance" in body


@pytest.mark.postgres_only
async def test_ingest_detail_shape(api_client: AsyncClient) -> None:
    """Detail response has the exact required top-level and line keys."""
    required_keys = {
        "id", "supplier_name", "supplier_abn", "customer_ref",
        "statement_date", "terms", "opening_balance", "closing_balance",
        "currency", "status", "our_ap_as_at", "balance_delta",
        "contact_id", "source_document_id", "extraction_meta", "lines",
    }
    line_keys = {
        "id", "line_date", "line_type", "reference", "description",
        "amount", "match_status", "matched_bill_id", "note",
    }

    company_id = await _default_company_id()
    stmt_id = uuid.uuid4()
    await _seed_statement(stmt_id=stmt_id, company_id=company_id, source_document_id=99, lines=True)

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy.orm import selectinload
            stmt_row = await session.get(
                SupplierStatement, stmt_id,
                options=[selectinload(SupplierStatement.lines)],
            )

        with patch(
            "saebooks.api.v1.statements.ingest_statement",
            new_callable=AsyncMock,
            return_value=stmt_row,
        ):
            r = await api_client.post(
                "/api/v1/statements/ingest",
                json={"paperless_document_id": 99},
            )
    finally:
        await _delete_statement(stmt_id)

    assert r.status_code == 201, r.text
    body = r.json()
    missing = required_keys - set(body.keys())
    assert not missing, f"Response missing keys: {missing}"
    if body["lines"]:
        missing_line = line_keys - set(body["lines"][0].keys())
        assert not missing_line, f"Line missing keys: {missing_line}"


# ---------------------------------------------------------------------------
# GET /statements
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_list_returns_envelope(api_client: AsyncClient) -> None:
    """GET /statements returns {items, total} envelope."""
    r = await api_client.get("/api/v1/statements")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)


@pytest.mark.postgres_only
async def test_list_item_shape(api_client: AsyncClient) -> None:
    """Items from GET /statements have the required ListItem keys."""
    company_id = await _default_company_id()
    stmt_id = uuid.uuid4()
    await _seed_statement(stmt_id=stmt_id, company_id=company_id, source_document_id=999)
    try:
        # Pin the active company to the seeded one — SupplierStatement is
        # CompanyScoped, and the full suite seeds multiple companies, so the
        # request's fallback "first active company" may differ. (prod code OK)
        r = await api_client.get(
            "/api/v1/statements", headers={"X-Company-Id": str(company_id)}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        matching = [i for i in body["items"] if i["id"] == str(stmt_id)]
        assert matching, "Seeded statement not found in list"
        item = matching[0]
        required_keys = {
            "id", "supplier_name", "statement_date", "status",
            "closing_balance", "our_ap_as_at", "balance_delta",
            "source_document_id", "exception_count",
        }
        missing = required_keys - set(item.keys())
        assert not missing, f"ListItem missing keys: {missing}"
    finally:
        await _delete_statement(stmt_id)


# ---------------------------------------------------------------------------
# GET /statements/{id}
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_get_detail_returns_lines(api_client: AsyncClient) -> None:
    """GET /statements/{id} returns detail with lines array."""
    company_id = await _default_company_id()
    stmt_id = uuid.uuid4()
    line_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(DEFAULT_TENANT_ID)
        stmt = SupplierStatement(
            id=stmt_id,
            tenant_id=DEFAULT_TENANT_ID,
            company_id=company_id,
            source_document_id=777,
            supplier_name="Detail Test Supplier",
            closing_balance=Decimal("200.00"),
            currency="AUD",
            status=StatementStatus.EXTRACTED.value,
        )
        session.add(stmt)
        await session.flush()
        line = SupplierStatementLine(
            id=line_id,
            tenant_id=DEFAULT_TENANT_ID,
            statement_id=stmt_id,
            line_date=date(2026, 5, 10),
            line_type="invoice",
            reference="INV-777",
            amount=Decimal("200.00"),
            match_status=StatementMatchStatus.MISSING_IN_BOOKS.value,
        )
        session.add(line)
        await session.commit()

    try:
        r = await api_client.get(
            f"/api/v1/statements/{stmt_id}",
            headers={"X-Company-Id": str(company_id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(stmt_id)
        assert isinstance(body["lines"], list)
        assert len(body["lines"]) == 1
        assert body["lines"][0]["id"] == str(line_id)
        assert body["lines"][0]["match_status"] == StatementMatchStatus.MISSING_IN_BOOKS.value
    finally:
        await _delete_statement(stmt_id)


# ---------------------------------------------------------------------------
# GET /statements/{unknown_id} → 404
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_get_unknown_id_returns_404(api_client: AsyncClient) -> None:
    """A random UUID returns 404."""
    r = await api_client.get(f"/api/v1/statements/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Read-only token rejected on POST (write-scope enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_post() -> None:
    """A read-scoped API token must receive 403 on POST /ingest."""
    from saebooks.models.user import User, UserRole
    from saebooks.services import api_tokens as token_svc
    from saebooks.services.companies import ensure_seed_company

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                sa_select(User).where(User.username == "pytest-stmt-ro-user")
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=DEFAULT_TENANT_ID,
                username="pytest-stmt-ro-user",
                role=UserRole.ADMIN.value,
            )
            session.add(user)
            await session.flush()

        company = await ensure_seed_company(session)
        _, cleartext = await token_svc.issue(
            session,
            user_id=user.id,
            company_id=company.id,
            name=f"stmt-ro-{uuid.uuid4().hex[:8]}",
            scopes=["read"],
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {cleartext}"},
    ) as ac:
        r = await ac.post(
            "/api/v1/statements/ingest",
            json={"paperless_document_id": 1},
        )
    assert r.status_code == 403, (
        f"Read-scoped token should get 403 on POST, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# RLS: tenant A statement invisible to tenant B
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_rls_statement_invisible_to_other_tenant() -> None:
    """A statement created under tenant A must not be visible to tenant B.

    Uses the SAEBOOKS_DEV_TENANT_ID override pattern from
    test_bills_transitions.py and test_reports_aged.py.
    """
    suffix = uuid.uuid4().hex[:8]
    tenant_a_id = uuid.uuid4()
    company_a_id = uuid.uuid4()
    stmt_id = uuid.uuid4()

    # Seed tenant A + company A + statement as the owner (BYPASSRLS) role.
    async with AsyncSessionLocal() as session:
        session.add(Tenant(id=tenant_a_id, name=f"stmt-rls-a-{suffix}", slug=f"stmt-rls-a-{suffix}"))
        await session.flush()
        session.add(Company(
            id=company_a_id,
            tenant_id=tenant_a_id,
            name=f"stmt-rls-a-co-{suffix}",
            base_currency="AUD",
            fin_year_start_month=7,
        ))
        await session.flush()
        session.add(SupplierStatement(
            id=stmt_id,
            tenant_id=tenant_a_id,
            company_id=company_a_id,
            source_document_id=8888,
            supplier_name="RLS Test Supplier",
            closing_balance=Decimal("100.00"),
            currency="AUD",
            status=StatementStatus.RECONCILED.value,
        ))
        await session.commit()

    token = current_token()

    # Probe GET /statements/{id} as tenant B (random UUID) — must 404.
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as ac:
            r_detail = await ac.get(f"/api/v1/statements/{stmt_id}")
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r_detail.status_code == 404, (
        f"Tenant B should not see tenant A's statement; got {r_detail.status_code}: {r_detail.text}"
    )

    # Probe GET /statements list as tenant B — stmt_id must be absent.
    tenant_b_id2 = str(uuid.uuid4())
    original2 = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id2
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as ac:
            r_list = await ac.get("/api/v1/statements")
    finally:
        if original2:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original2
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B with a random UUID has no company → get_active_company_id
    # raises 404 before any data is returned. Both 404 (no company) and
    # 200-with-absent-id prove that tenant A's statement is invisible.
    if r_list.status_code == 200:
        ids_returned = {i["id"] for i in r_list.json()["items"]}
        assert str(stmt_id) not in ids_returned, (
            f"Tenant B should not see tenant A's statement {stmt_id} in list"
        )
    else:
        assert r_list.status_code == 404, (
            f"Expected 200 or 404 for tenant B list, got {r_list.status_code}: {r_list.text}"
        )

    # Cleanup.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM supplier_statements WHERE id = :id").bindparams(id=stmt_id)
        )
        await session.execute(
            text("DELETE FROM companies WHERE id = :id").bindparams(id=company_a_id)
        )
        await session.execute(
            text("DELETE FROM tenants WHERE id = :id").bindparams(id=tenant_a_id)
        )
        await session.commit()


# ===========================================================================
# Phase 3 additions — action endpoints (POST draft-missing-bill / dismiss /
# confirm). Appended to the Phase 1/2 suite; shares its imports and helpers.
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 3 helpers
# ---------------------------------------------------------------------------


async def _p3_default_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                sa_select(Company).where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if company is None:
            pytest.skip("No active company in default tenant")
        return company.id


async def _seed_p3_statement(
    *,
    stmt_id: uuid.UUID,
    company_id: uuid.UUID,
    status: str = StatementStatus.NEEDS_REVIEW.value,
    contact_id: uuid.UUID | None = None,
) -> None:
    """Insert a SupplierStatement for Phase 3 tests."""
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(DEFAULT_TENANT_ID)
        stmt = SupplierStatement(
            id=stmt_id,
            tenant_id=DEFAULT_TENANT_ID,
            company_id=company_id,
            source_document_id=9900,
            supplier_name="Phase3 Test Supplier",
            statement_date=date(2026, 5, 31),
            closing_balance=Decimal("1100.00"),
            currency="AUD",
            status=status,
            contact_id=contact_id,
        )
        session.add(stmt)
        await session.commit()


async def _seed_p3_line(
    *,
    line_id: uuid.UUID,
    stmt_id: uuid.UUID,
    match_status: str = StatementMatchStatus.MISSING_IN_BOOKS.value,
    reference: str = "INV-P3-001",
) -> None:
    """Insert a SupplierStatementLine for Phase 3 tests."""
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(DEFAULT_TENANT_ID)
        line = SupplierStatementLine(
            id=line_id,
            tenant_id=DEFAULT_TENANT_ID,
            statement_id=stmt_id,
            line_date=date(2026, 5, 1),
            line_type="invoice",
            reference=reference,
            description="Phase 3 test line",
            amount=Decimal("1100.00"),
            match_status=match_status,
        )
        session.add(line)
        await session.commit()


async def _delete_p3_statement(stmt_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM supplier_statement_lines WHERE statement_id = :id").bindparams(id=stmt_id)
        )
        await session.execute(
            text("DELETE FROM supplier_statements WHERE id = :id").bindparams(id=stmt_id)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Phase 3 fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def p3_api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def p3_readonly_client() -> AsyncClient:
    """API client with a read-scoped API token for write-rejection tests."""
    from saebooks.models.user import User, UserRole
    from saebooks.services import api_tokens as token_svc
    from saebooks.services.companies import ensure_seed_company

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                sa_select(User).where(User.username == "pytest-p3-ro-user")
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=DEFAULT_TENANT_ID,
                username="pytest-p3-ro-user",
                role=UserRole.ADMIN.value,
            )
            session.add(user)
            await session.flush()

        company = await ensure_seed_company(session)
        _, cleartext = await token_svc.issue(
            session,
            user_id=user.id,
            company_id=company.id,
            name=f"p3-ro-{uuid.uuid4().hex[:8]}",
            scopes=["read"],
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {cleartext}"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /{id}/draft-missing-bill — 201, bill_id, line updated
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_draft_missing_bill_returns_201_and_bill_id(p3_api_client: AsyncClient) -> None:
    """POST /draft-missing-bill on a missing_in_books line → 201 with bill_id."""
    from saebooks.models.bill import Bill, BillStatus

    company_id = await _p3_default_company_id()
    stmt_id = uuid.uuid4()
    line_id = uuid.uuid4()

    await _seed_p3_statement(stmt_id=stmt_id, company_id=company_id)
    await _seed_p3_line(line_id=line_id, stmt_id=stmt_id)

    try:
        r = await p3_api_client.post(
            f"/api/v1/statements/{stmt_id}/draft-missing-bill",
            json={"line_id": str(line_id)},
            headers={"X-Company-Id": str(company_id)},
        )
    finally:
        await _delete_p3_statement(stmt_id)

    assert r.status_code == 201, r.text
    body = r.json()
    assert "bill_id" in body, f"Missing bill_id in response: {body}"
    assert "statement" in body, f"Missing statement in response: {body}"

    bill_uuid = uuid.UUID(body["bill_id"])

    # Verify statement detail shape.
    stmt_detail = body["statement"]
    assert stmt_detail["id"] == str(stmt_id)
    assert "lines" in stmt_detail

    # Verify a DRAFT Bill with correct supplier_reference exists in DB.
    async with AsyncSessionLocal() as session:
        bill = await session.get(Bill, bill_uuid)
        assert bill is not None, "Bill not found in DB after draft-missing-bill"
        assert bill.status == BillStatus.DRAFT.value, f"Expected DRAFT, got {bill.status}"
        assert bill.supplier_reference == "INV-P3-001"


@pytest.mark.postgres_only
async def test_draft_missing_bill_line_is_now_matched(p3_api_client: AsyncClient) -> None:
    """After draft-missing-bill, the line is matched and matched_bill_id is set."""
    company_id = await _p3_default_company_id()
    stmt_id = uuid.uuid4()
    line_id = uuid.uuid4()

    await _seed_p3_statement(stmt_id=stmt_id, company_id=company_id)
    await _seed_p3_line(line_id=line_id, stmt_id=stmt_id)

    try:
        r = await p3_api_client.post(
            f"/api/v1/statements/{stmt_id}/draft-missing-bill",
            json={"line_id": str(line_id)},
            headers={"X-Company-Id": str(company_id)},
        )
    finally:
        await _delete_p3_statement(stmt_id)

    assert r.status_code == 201, r.text
    body = r.json()
    bill_id_str = body["bill_id"]

    # The statement detail in the response must show the line as matched.
    lines = body["statement"]["lines"]
    assert lines, "No lines in returned statement detail"
    matched = [ln for ln in lines if ln["id"] == str(line_id)]
    assert matched, f"Line {line_id} not in returned statement lines"
    ln = matched[0]
    assert ln["match_status"] == StatementMatchStatus.MATCHED.value
    assert ln["matched_bill_id"] == bill_id_str
    assert "draft bill created" in (ln["note"] or "")


@pytest.mark.postgres_only
async def test_draft_missing_bill_non_missing_line_returns_422(p3_api_client: AsyncClient) -> None:
    """POST /draft-missing-bill on an already-matched line → 422."""
    company_id = await _p3_default_company_id()
    stmt_id = uuid.uuid4()
    line_id = uuid.uuid4()

    await _seed_p3_statement(stmt_id=stmt_id, company_id=company_id)
    await _seed_p3_line(
        line_id=line_id,
        stmt_id=stmt_id,
        match_status=StatementMatchStatus.MATCHED.value,
    )

    try:
        r = await p3_api_client.post(
            f"/api/v1/statements/{stmt_id}/draft-missing-bill",
            json={"line_id": str(line_id)},
            headers={"X-Company-Id": str(company_id)},
        )
    finally:
        await _delete_p3_statement(stmt_id)

    assert r.status_code == 422, r.text


@pytest.mark.postgres_only
async def test_draft_missing_bill_unknown_statement_returns_404(p3_api_client: AsyncClient) -> None:
    """POST /draft-missing-bill with an unknown statement UUID → 404."""
    company_id = await _p3_default_company_id()
    r = await p3_api_client.post(
        f"/api/v1/statements/{uuid.uuid4()}/draft-missing-bill",
        json={"line_id": str(uuid.uuid4())},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# POST /{id}/dismiss
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_dismiss_sets_status_dismissed(p3_api_client: AsyncClient) -> None:
    """POST /dismiss returns detail with status=dismissed."""
    company_id = await _p3_default_company_id()
    stmt_id = uuid.uuid4()
    await _seed_p3_statement(stmt_id=stmt_id, company_id=company_id)

    try:
        r = await p3_api_client.post(
            f"/api/v1/statements/{stmt_id}/dismiss",
            headers={"X-Company-Id": str(company_id)},
        )
    finally:
        await _delete_p3_statement(stmt_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(stmt_id)
    assert body["status"] == StatementStatus.DISMISSED.value


@pytest.mark.postgres_only
async def test_dismiss_unknown_statement_returns_404(p3_api_client: AsyncClient) -> None:
    """POST /dismiss with unknown UUID → 404."""
    company_id = await _p3_default_company_id()
    r = await p3_api_client.post(
        f"/api/v1/statements/{uuid.uuid4()}/dismiss",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# POST /{id}/confirm
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_confirm_sets_status_reconciled(p3_api_client: AsyncClient) -> None:
    """POST /confirm returns detail with status=reconciled."""
    company_id = await _p3_default_company_id()
    stmt_id = uuid.uuid4()
    await _seed_p3_statement(stmt_id=stmt_id, company_id=company_id)

    try:
        r = await p3_api_client.post(
            f"/api/v1/statements/{stmt_id}/confirm",
            headers={"X-Company-Id": str(company_id)},
        )
    finally:
        await _delete_p3_statement(stmt_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(stmt_id)
    assert body["status"] == StatementStatus.RECONCILED.value


@pytest.mark.postgres_only
async def test_confirm_unknown_statement_returns_404(p3_api_client: AsyncClient) -> None:
    """POST /confirm with unknown UUID → 404."""
    company_id = await _p3_default_company_id()
    r = await p3_api_client.post(
        f"/api/v1/statements/{uuid.uuid4()}/confirm",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Read-only token rejected (403) on each action endpoint
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_draft_missing_bill(
    p3_readonly_client: AsyncClient,
) -> None:
    """Read-scoped token must receive 403 on POST /draft-missing-bill."""
    r = await p3_readonly_client.post(
        f"/api/v1/statements/{uuid.uuid4()}/draft-missing-bill",
        json={"line_id": str(uuid.uuid4())},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_dismiss(
    p3_readonly_client: AsyncClient,
) -> None:
    """Read-scoped token must receive 403 on POST /dismiss."""
    r = await p3_readonly_client.post(f"/api/v1/statements/{uuid.uuid4()}/dismiss")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_confirm(
    p3_readonly_client: AsyncClient,
) -> None:
    """Read-scoped token must receive 403 on POST /confirm."""
    r = await p3_readonly_client.post(f"/api/v1/statements/{uuid.uuid4()}/confirm")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_ingest_serializes_after_real_session_commit(api_client: AsyncClient) -> None:
    """Regression (#28 P4): the live ingest 500ed with MissingGreenlet because
    ingest_statement commits internally and the handler then re-loaded the
    (expired, identity-mapped) row via session.get(), whose selectinload was
    not re-applied -> `lines` lazy-loaded during Pydantic serialization. This
    mock mimics the REAL behaviour: it creates + commits the statement in the
    HANDLER's session and returns the expired instance. The handler must still
    serialize it (fresh SELECT + populate_existing), returning 201 with lines.
    """
    company_id = await _default_company_id()
    stmt_id = uuid.uuid4()
    line_id = uuid.uuid4()

    async def _fake_ingest(session, *, tenant_id, company_id, paperless_document_id, settings):
        stmt = SupplierStatement(
            id=stmt_id, tenant_id=DEFAULT_TENANT_ID, company_id=company_id,
            source_document_id=paperless_document_id,
            supplier_name="Greenlet Test Pty Ltd", closing_balance=Decimal("100.00"),
            currency="AUD", status=StatementStatus.EXTRACTED.value,
        )
        session.add(stmt)
        await session.flush()
        session.add(SupplierStatementLine(
            id=line_id, tenant_id=DEFAULT_TENANT_ID, statement_id=stmt_id,
            line_type="invoice", reference="GL-1", amount=Decimal("100.00"),
            match_status=StatementMatchStatus.MISSING_IN_BOOKS.value,
        ))
        await session.commit()          # leaves stmt expired in the identity map
        return stmt

    try:
        with patch("saebooks.api.v1.statements.ingest_statement", new=_fake_ingest):
            r = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id)},
                json={"paperless_document_id": 13287},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["id"] == str(stmt_id)
        assert len(body["lines"]) == 1
        assert body["lines"][0]["reference"] == "GL-1"
    finally:
        await _delete_statement(stmt_id)


# ===========================================================================
# #28 defect 7 — real end-to-end ingest API test (no ingest_statement mock)
# #28 defect 8 — idempotency store_response str-vs-BYTEA
#
# These drive POST /api/v1/statements/ingest through the REAL ingest pipeline,
# patching only the boundary: PaperlessClient (a fake) + extract's _call_llm
# (an AsyncMock). This exercises the post-reconcile re-SELECT against the real
# DB state — the part a mocked ingest_statement can never cover.
# ===========================================================================


import json as _json_e2e

from saebooks.config import Settings as _SettingsE2E
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact, ContactType
from saebooks.services.statements import extract as _extract_mod_e2e


class _FakePaperlessE2E:
    """Stand-in PaperlessClient returning enough OCR to skip the vision path."""
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def get_document(self, document_id: int) -> dict:
        return {
            "id": document_id,
            "title": f"Statement {document_id}.pdf",
            "content": (
                "E2E Recon Supplier Pty Ltd\nABN: 11 222 333 444\n"
                "Statement Date: 31/05/2026\nBalance Due: 999.00\n"
            ),
        }


def _e2e_settings() -> _SettingsE2E:
    return _SettingsE2E(
        DATABASE_URL="postgresql+asyncpg://saebooks_test:saebooks_test_pw@db:5432/saebooks_test",
        SAEBOOKS_APP_DB_PASSWORD="saebooks_app_test_pw",
        PAPERLESS_API_TOKEN="fake-token",
        PAPERLESS_URL="http://paperless:8000",
        PAPERLESS_API_URL="http://paperless:8000",
        STATEMENT_LLM_BASE="http://litellm:4000/v1",
        STATEMENT_LLM_MODEL="claude-sonnet-4-6",
        STATEMENT_LLM_MODEL_ESCALATION="claude-opus-4-7",
        STATEMENT_LLM_API_KEY="test-key",
        SAEBOOKS_SQL_RO_PASSWORD="saebooks_sql_ro_test_pw",
    )


def _e2e_llm_response() -> str:
    """A statement that yields a MISSING_IN_BOOKS line (INV-OTHER, not in our
    books). Combined with a seeded NOT-on-statement bill it produces a
    NOT_ON_STATEMENT synthetic line too — populating recon_counts."""
    return _json_e2e.dumps({
        "supplier_name": "E2E Recon Supplier Pty Ltd",
        "supplier_abn": "11 222 333 444",
        "customer_ref": "SAE-E2E",
        "statement_date": "2026-05-31",
        "terms": "30 Days",
        "closing_balance": 999.00,
        "opening_balance": 0.00,
        "lines": [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-OTHER",
             "description": "Not in our books", "amount": 999.00},
        ],
    })


async def _seed_e2e_orphan_bill(company_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a supplier contact + a POSTED bill (ref INV-ORPHAN) that is NOT on
    the statement, so reconcile emits a NOT_ON_STATEMENT synthetic line.
    Returns (contact_id, bill_id) for cleanup."""
    from saebooks.models.account import Account
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(DEFAULT_TENANT_ID)
        session.info["company_id"] = company_id
        contact = Contact(
            tenant_id=DEFAULT_TENANT_ID, company_id=company_id,
            name="E2E Recon Supplier Pty Ltd", contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.flush()
        acct = (await session.execute(
            sa_select(Account).where(Account.company_id == company_id).limit(1)
        )).scalars().first()
        bill = Bill(
            tenant_id=DEFAULT_TENANT_ID, company_id=company_id, contact_id=contact.id,
            supplier_reference="INV-ORPHAN", issue_date=date(2026, 5, 9),
            due_date=date(2026, 6, 9), status=BillStatus.POSTED,
            subtotal=Decimal("500.00"), tax_total=Decimal("0.00"),
            total=Decimal("500.00"), amount_paid=Decimal("0.00"),
        )
        if acct is not None:
            session.add(bill)
            await session.flush()
        await session.commit()
        return contact.id, bill.id


async def _cleanup_e2e(contact_id: uuid.UUID, bill_id: uuid.UUID, doc_id: int) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM supplier_statement_lines WHERE statement_id IN "
            "(SELECT id FROM supplier_statements WHERE source_document_id = :d)"
        ).bindparams(d=doc_id))
        await session.execute(text(
            "DELETE FROM supplier_statements WHERE source_document_id = :d"
        ).bindparams(d=doc_id))
        await session.execute(text("DELETE FROM bills WHERE id = :id").bindparams(id=bill_id))
        await session.execute(text("DELETE FROM contacts WHERE id = :id").bindparams(id=contact_id))
        await session.commit()


async def _seed_e2e_company() -> tuple[uuid.UUID, uuid.UUID]:
    """Provision a dedicated company (+ one Account) under DEFAULT_TENANT_ID
    for the hermetic E2E recon test below.

    ``_default_company_id()`` picks "the first active company for the
    tenant" with no ``ORDER BY`` — nondeterministic once the full suite
    has created other companies under the same DEFAULT_TENANT_ID (e.g.
    ``tests/api/v1/test_cross_company_isolation.py``'s ``other_company_id``
    fixture). ``_seed_e2e_orphan_bill`` only persists its orphan Bill when
    an Account already exists in the target company (``if acct is not
    None``) — if the shared "first company" picked under full-suite
    ordering happens to have zero accounts, the orphan bill silently never
    gets created and the NOT_ON_STATEMENT synthetic line this test asserts
    on never appears. Own company + own account removes the dependency on
    which company any other test happens to have created first.
    Returns (company_id, account_id) for cleanup.
    """
    from saebooks.models.account import Account, AccountType

    company_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"stmt-e2e-co-{suffix}",
                base_currency="AUD",
                fin_year_start_month=7,
            )
        )
        await session.flush()
        session.add(
            Account(
                id=account_id,
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company_id,
                code=f"E2E{suffix[:5].upper()}",
                name=f"stmt-e2e account {suffix}",
                account_type=AccountType.EXPENSE,
            )
        )
        await session.commit()
    return company_id, account_id


async def _cleanup_e2e_company(company_id: uuid.UUID, account_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM accounts WHERE id = :id").bindparams(id=account_id))
        await session.execute(text("DELETE FROM companies WHERE id = :id").bindparams(id=company_id))
        await session.commit()


@pytest.mark.postgres_only
async def test_ingest_e2e_real_pipeline_returns_lines(api_client: AsyncClient) -> None:
    """#28 defect 7: drive the REAL ingest pipeline (no ingest_statement mock);
    patch only PaperlessClient + extract._call_llm. Assert 201, lines present,
    and recon_counts populated — exercising the post-reconcile re-SELECT.

    Hermetic: uses its own company (``_seed_e2e_company``), not the
    suite-shared ``_default_company_id()`` — see that helper's docstring
    for why the shared lookup made this test order-dependent.
    """
    company_id, account_id = await _seed_e2e_company()
    doc_id = 24301
    contact_id, bill_id = await _seed_e2e_orphan_bill(company_id)

    try:
        with (
            patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessE2E),
            patch.object(_extract_mod_e2e, "_call_llm",
                         new=AsyncMock(return_value=_e2e_llm_response())),
        ):
            r = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id)},
                json={"paperless_document_id": doc_id},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_document_id"] == doc_id
        assert isinstance(body["lines"], list)
        assert len(body["lines"]) >= 1
        # The NOT_ON_STATEMENT synthetic for the orphan bill must be present.
        statuses = {ln["match_status"] for ln in body["lines"]}
        assert StatementMatchStatus.NOT_ON_STATEMENT.value in statuses, statuses
        # recon_counts populated in extraction_meta.
        assert body["extraction_meta"].get("recon_counts"), body["extraction_meta"]
    finally:
        await _cleanup_e2e(contact_id, bill_id, doc_id)
        await _cleanup_e2e_company(company_id, account_id)


@pytest.mark.postgres_only
async def test_ingest_idempotency_replay_same_body(api_client: AsyncClient) -> None:
    """#28 defect 8: same X-Idempotency-Key + same body twice → the 2nd is a
    201 replay of the identical detail. This round-trips response_body through
    the BYTEA column, which fails if store_response is handed a str instead of
    bytes."""
    company_id = await _default_company_id()
    doc_id = 24302
    contact_id, bill_id = await _seed_e2e_orphan_bill(company_id)
    key = str(uuid.uuid4())

    try:
        with (
            patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessE2E),
            patch.object(_extract_mod_e2e, "_call_llm",
                         new=AsyncMock(return_value=_e2e_llm_response())),
        ):
            r1 = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id), "X-Idempotency-Key": key},
                json={"paperless_document_id": doc_id},
            )
            assert r1.status_code == 201, r1.text
            first = r1.json()

            r2 = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id), "X-Idempotency-Key": key},
                json={"paperless_document_id": doc_id},
            )
        assert r2.status_code == 201, r2.text
        replay = r2.json()
        # Identical detail on replay (proves the BYTEA round-trip succeeded).
        assert replay["id"] == first["id"]
        assert replay["source_document_id"] == first["source_document_id"]
        assert replay == first
    finally:
        await _cleanup_e2e(contact_id, bill_id, doc_id)


@pytest.mark.postgres_only
async def test_ingest_idempotency_conflict_different_body(api_client: AsyncClient) -> None:
    """#28 defect 8: same X-Idempotency-Key + a DIFFERENT body → 422 conflict."""
    company_id = await _default_company_id()
    doc_id = 24303
    contact_id, bill_id = await _seed_e2e_orphan_bill(company_id)
    key = str(uuid.uuid4())

    try:
        with (
            patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessE2E),
            patch.object(_extract_mod_e2e, "_call_llm",
                         new=AsyncMock(return_value=_e2e_llm_response())),
        ):
            r1 = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id), "X-Idempotency-Key": key},
                json={"paperless_document_id": doc_id},
            )
            assert r1.status_code == 201, r1.text

            r2 = await api_client.post(
                "/api/v1/statements/ingest",
                headers={"X-Company-Id": str(company_id), "X-Idempotency-Key": key},
                json={"paperless_document_id": doc_id + 1},  # different body
            )
        assert r2.status_code == 422, r2.text
        assert r2.json()["code"] == "idempotency_key_conflict"
    finally:
        await _cleanup_e2e(contact_id, bill_id, doc_id)
        await _cleanup_e2e(contact_id, bill_id, doc_id + 1)
