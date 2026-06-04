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
from sqlalchemy import select as sa_select, text

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
