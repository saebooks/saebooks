"""API contract tests — /api/v1/statement-templates + /api/v1/statements?contact_id.

Covers (P4, Gitea #28):
* POST /statement-templates → 201 + appears in GET list
* POST without prompt_hint → 422
* POST without any match key → 422
* DELETE → 204 then absent from GET list
* Read-only token rejected (403) on POST + DELETE
* RLS: template created under tenant A invisible to tenant B
* GET /api/v1/statements?contact_id=<id> returns only that supplier's statements
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select as sa_select
from sqlalchemy import text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.supplier_statement import StatementStatus, SupplierStatement
from saebooks.models.supplier_statement_template import SupplierStatementTemplate
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers
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


async def _delete_template(tmpl_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM supplier_statement_templates WHERE id = :id").bindparams(id=tmpl_id)
        )
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


async def _seed_statement(
    *,
    stmt_id: uuid.UUID,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
    company_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    status: str = StatementStatus.RECONCILED.value,
) -> None:
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_id)
        stmt = SupplierStatement(
            id=stmt_id,
            tenant_id=tenant_id,
            company_id=company_id,
            source_document_id=1234,
            supplier_name="P4 Test Supplier",
            closing_balance=Decimal("500.00"),
            currency="AUD",
            status=status,
            contact_id=contact_id,
        )
        session.add(stmt)
        await session.commit()


@pytest.fixture
async def p4_readonly_client() -> AsyncClient:
    """API client with a read-scoped API token."""
    from saebooks.models.user import User, UserRole
    from saebooks.services import api_tokens as token_svc
    from saebooks.services.companies import ensure_seed_company

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                sa_select(
                    __import__("saebooks.models.user", fromlist=["User"]).User
                ).where(
                    __import__("saebooks.models.user", fromlist=["User"]).User.username
                    == "pytest-p4-ro-user"
                )
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=DEFAULT_TENANT_ID,
                username="pytest-p4-ro-user",
                role=UserRole.ADMIN.value,
            )
            session.add(user)
            await session.flush()

        company = await ensure_seed_company(session)
        _, cleartext = await token_svc.issue(
            session,
            user_id=user.id,
            company_id=company.id,
            name=f"p4-ro-{uuid.uuid4().hex[:8]}",
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
# POST /statement-templates → 201
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_create_template_returns_201(api_client: AsyncClient) -> None:
    """POST /statement-templates with valid body → 201 with template object."""
    company_id = await _default_company_id()
    payload = {
        "supplier_name": "Acme Fuel Cards Pty Ltd",
        "supplier_abn": "12 345 678 901",
        "prompt_hint": "Amounts are in the rightmost column; use page-1 summary only.",
        "page_scope": "page_1_only",
    }
    r = await api_client.post(
        "/api/v1/statement-templates",
        json=payload,
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    tmpl_id = uuid.UUID(body["id"])
    try:
        assert body["supplier_name"] == payload["supplier_name"]
        assert body["supplier_abn"] == payload["supplier_abn"]
        assert body["prompt_hint"] == payload["prompt_hint"]
        assert body["page_scope"] == payload["page_scope"]
        assert body["active"] is True
        assert "created_at" in body
        assert body["contact_id"] is None
    finally:
        await _delete_template(tmpl_id)


@pytest.mark.postgres_only
async def test_create_template_appears_in_list(api_client: AsyncClient) -> None:
    """Template created via POST appears in GET list."""
    company_id = await _default_company_id()
    r = await api_client.post(
        "/api/v1/statement-templates",
        json={
            "supplier_name": "List-Check Supplier",
            "prompt_hint": "Check list inclusion.",
        },
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 201, r.text
    tmpl_id = uuid.UUID(r.json()["id"])

    try:
        r_list = await api_client.get(
            "/api/v1/statement-templates",
            headers={"X-Company-Id": str(company_id)},
        )
        assert r_list.status_code == 200, r_list.text
        body = r_list.json()
        assert "items" in body
        ids_in_list = [i["id"] for i in body["items"]]
        assert str(tmpl_id) in ids_in_list, (
            f"Created template {tmpl_id} not found in list: {ids_in_list}"
        )
    finally:
        await _delete_template(tmpl_id)


# ---------------------------------------------------------------------------
# POST /statement-templates validation failures → 422
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_create_template_without_prompt_hint_returns_422(
    api_client: AsyncClient,
) -> None:
    """POST without prompt_hint → 422."""
    company_id = await _default_company_id()
    r = await api_client.post(
        "/api/v1/statement-templates",
        json={"supplier_name": "Some Supplier"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 422, r.text


@pytest.mark.postgres_only
async def test_create_template_empty_prompt_hint_returns_422(
    api_client: AsyncClient,
) -> None:
    """POST with empty prompt_hint string → 422."""
    company_id = await _default_company_id()
    r = await api_client.post(
        "/api/v1/statement-templates",
        json={"supplier_name": "Some Supplier", "prompt_hint": ""},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 422, r.text


@pytest.mark.postgres_only
async def test_create_template_without_any_match_key_returns_422(
    api_client: AsyncClient,
) -> None:
    """POST with prompt_hint but no match key → 422."""
    company_id = await _default_company_id()
    r = await api_client.post(
        "/api/v1/statement-templates",
        json={"prompt_hint": "Some hint but no supplier identity"},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# DELETE /statement-templates/{id} → 204
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_delete_template_returns_204(api_client: AsyncClient) -> None:
    """DELETE /statement-templates/{id} → 204."""
    company_id = await _default_company_id()
    r_create = await api_client.post(
        "/api/v1/statement-templates",
        json={"supplier_name": "Delete Test Supplier", "prompt_hint": "Delete me."},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r_create.status_code == 201, r_create.text
    tmpl_id = r_create.json()["id"]

    r_del = await api_client.delete(
        f"/api/v1/statement-templates/{tmpl_id}",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r_del.status_code == 204, r_del.text


@pytest.mark.postgres_only
async def test_delete_template_then_absent_from_list(api_client: AsyncClient) -> None:
    """After DELETE, template absent from GET list."""
    company_id = await _default_company_id()
    r_create = await api_client.post(
        "/api/v1/statement-templates",
        json={"supplier_name": "Absent Test Supplier", "prompt_hint": "Gone soon."},
        headers={"X-Company-Id": str(company_id)},
    )
    assert r_create.status_code == 201, r_create.text
    tmpl_id = r_create.json()["id"]

    await api_client.delete(
        f"/api/v1/statement-templates/{tmpl_id}",
        headers={"X-Company-Id": str(company_id)},
    )

    r_list = await api_client.get(
        "/api/v1/statement-templates",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r_list.status_code == 200, r_list.text
    ids_in_list = [i["id"] for i in r_list.json()["items"]]
    assert tmpl_id not in ids_in_list, (
        f"Deleted template {tmpl_id} still appears in list"
    )


@pytest.mark.postgres_only
async def test_delete_unknown_template_returns_404(api_client: AsyncClient) -> None:
    """DELETE with unknown UUID → 404."""
    company_id = await _default_company_id()
    r = await api_client.delete(
        f"/api/v1/statement-templates/{uuid.uuid4()}",
        headers={"X-Company-Id": str(company_id)},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Read-only token rejected (403) on POST + DELETE
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_create(
    p4_readonly_client: AsyncClient,
) -> None:
    """Read-scoped token → 403 on POST /statement-templates."""
    r = await p4_readonly_client.post(
        "/api/v1/statement-templates",
        json={"supplier_name": "Should Fail", "prompt_hint": "No write."},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.postgres_only
async def test_read_only_token_rejected_on_delete(
    p4_readonly_client: AsyncClient,
) -> None:
    """Read-scoped token → 403 on DELETE /statement-templates/{id}."""
    r = await p4_readonly_client.delete(
        f"/api/v1/statement-templates/{uuid.uuid4()}"
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# RLS: template created under tenant A invisible to tenant B
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_rls_template_invisible_to_other_tenant() -> None:
    """A template created under tenant A must not be visible to tenant B."""
    suffix = uuid.uuid4().hex[:8]
    tenant_a_id = uuid.uuid4()
    company_a_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.add(Tenant(id=tenant_a_id, name=f"tmpl-rls-a-{suffix}", slug=f"tmpl-rls-a-{suffix}"))
        await session.flush()
        session.add(Company(
            id=company_a_id,
            tenant_id=tenant_a_id,
            name=f"tmpl-rls-a-co-{suffix}",
            base_currency="AUD",
            fin_year_start_month=7,
        ))
        await session.flush()
        # Insert the template directly as the BYPASSRLS superuser session.
        session.info["tenant_id"] = str(tenant_a_id)
        tmpl = SupplierStatementTemplate(
            tenant_id=tenant_a_id,
            company_id=company_a_id,
            supplier_name="RLS Test Supplier",
            prompt_hint="This should be invisible to tenant B.",
            active=True,
        )
        session.add(tmpl)
        await session.commit()
        tmpl_id = tmpl.id

    token = current_token()
    tenant_b_id = str(uuid.uuid4())

    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as ac:
            r_list = await ac.get(
                "/api/v1/statement-templates",
                headers={"X-Company-Id": str(company_a_id)},
            )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B with no company in its own tenant → 404 from get_active_company_id,
    # or 200 with an empty list (company_id mismatch blocks rows). Either proves
    # tenant A's template is invisible.
    if r_list.status_code == 200:
        ids_returned = [i["id"] for i in r_list.json()["items"]]
        assert str(tmpl_id) not in ids_returned, (
            f"Tenant B should not see tenant A's template {tmpl_id} in list"
        )
    else:
        assert r_list.status_code == 404, (
            f"Expected 200 or 404 for tenant B list, got {r_list.status_code}: {r_list.text}"
        )

    # Cleanup.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM supplier_statement_templates WHERE id = :id").bindparams(id=tmpl_id)
        )
        await session.execute(
            text("DELETE FROM companies WHERE id = :id").bindparams(id=company_a_id)
        )
        await session.execute(
            text("DELETE FROM tenants WHERE id = :id").bindparams(id=tenant_a_id)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# GET /api/v1/statements?contact_id filter
# ---------------------------------------------------------------------------


async def _seed_contact(
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
    name: str,
) -> uuid.UUID:
    """Insert a minimal Contact row and return its id."""
    from saebooks.models.contact import Contact, ContactType

    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_id)
        contact = Contact(
            tenant_id=tenant_id,
            company_id=company_id,
            name=name,
            contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.commit()
        return contact.id


async def _delete_contact(contact_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM contacts WHERE id = :id").bindparams(id=contact_id)
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_statements_list_filter_by_contact_id(api_client: AsyncClient) -> None:
    """GET /statements?contact_id=<id> returns only that supplier's statements."""
    company_id = await _default_company_id()

    # Create real contact rows (supplier_statements.contact_id is a FK).
    target_contact_id = await _seed_contact(
        company_id=company_id, name="P4 Target Supplier"
    )
    other_contact_id = await _seed_contact(
        company_id=company_id, name="P4 Other Supplier"
    )

    stmt_target_id = uuid.uuid4()
    stmt_other_id = uuid.uuid4()

    await _seed_statement(
        stmt_id=stmt_target_id,
        company_id=company_id,
        contact_id=target_contact_id,
    )
    await _seed_statement(
        stmt_id=stmt_other_id,
        company_id=company_id,
        contact_id=other_contact_id,
    )

    try:
        r = await api_client.get(
            "/api/v1/statements",
            params={"contact_id": str(target_contact_id)},
            headers={"X-Company-Id": str(company_id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "items" in body
        ids_returned = {i["id"] for i in body["items"]}
        assert str(stmt_target_id) in ids_returned, (
            f"Target statement {stmt_target_id} not in filtered list"
        )
        assert str(stmt_other_id) not in ids_returned, (
            f"Other-contact statement {stmt_other_id} leaked into contact-filtered list"
        )
    finally:
        await _delete_statement(stmt_target_id)
        await _delete_statement(stmt_other_id)
        await _delete_contact(target_contact_id)
        await _delete_contact(other_contact_id)


@pytest.mark.postgres_only
async def test_statements_list_no_contact_filter_returns_all(
    api_client: AsyncClient,
) -> None:
    """GET /statements without contact_id returns all statements for the company."""
    company_id = await _default_company_id()
    # No contact_id — seed with NULL to avoid the FK requirement.
    stmt_id = uuid.uuid4()
    await _seed_statement(stmt_id=stmt_id, company_id=company_id, contact_id=None)

    try:
        r = await api_client.get(
            "/api/v1/statements",
            headers={"X-Company-Id": str(company_id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        ids_returned = {i["id"] for i in body["items"]}
        assert str(stmt_id) in ids_returned, (
            f"Statement {stmt_id} missing from unfiltered list"
        )
    finally:
        await _delete_statement(stmt_id)
