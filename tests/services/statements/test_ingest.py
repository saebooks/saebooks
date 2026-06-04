"""Tests for services/statements/ingest.py.

Uses a real Postgres DB (pytestmark = postgres_only) with the seed company.
Mocks: PaperlessClient (no real Paperless needed) + extract._call_llm
(no real LLM needed).
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.supplier_statement import (
    StatementMatchStatus,
    StatementStatus,
    SupplierStatement,
    SupplierStatementLine,
)
from saebooks.services.statements import extract as extract_mod
from saebooks.services.statements.ingest import ingest_statement

pytestmark = pytest.mark.postgres_only

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (
            await s.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert co is not None, "No seed company found"
        return co.id


def _llm_response(
    *,
    supplier_name: str | None = "Motion Australia Pty Ltd",
    closing_balance: float = 1100.00,
    lines: list | None = None,
) -> str:
    if lines is None:
        lines = [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-1001",
             "description": "Bearings", "amount": closing_balance},
        ]
    return json.dumps({
        "supplier_name": supplier_name,
        "supplier_abn": "32 000 143 608",
        "customer_ref": "SAE-0042",
        "statement_date": "2026-05-31",
        "terms": "30 Days",
        "closing_balance": closing_balance,
        "opening_balance": 0.00,
        "lines": lines,
    })


def _fake_settings(company_id: uuid.UUID | None = None) -> Settings:
    return Settings(
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


class _FakePaperlessClient:
    """Stand-in for PaperlessClient."""
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def get_document(self, document_id: int) -> dict:
        return {
            "id": document_id,
            "title": f"Statement {document_id}.pdf",
            "content": "Motion Australia Pty Ltd\nABN: 32 000 143 608\nStatement Date: 31/05/2026\n...",
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_creates_statement_and_lines():
    """Happy path: ingest creates a SupplierStatement + lines in the DB."""
    company_id = await _seed_company_id()
    settings = _fake_settings(company_id)
    doc_id = 9001

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_llm_response())),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            session.info["company_id"] = company_id

            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()
            stmt_id = stmt.id

    # Verify persisted
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = _TENANT
        loaded = await session.get(SupplierStatement, stmt_id)
        assert loaded is not None
        assert loaded.supplier_name == "Motion Australia Pty Ltd"
        assert loaded.source_document_id == doc_id
        assert loaded.tenant_id == _TENANT
        assert loaded.company_id == company_id
        assert loaded.extraction_meta is not None
        assert loaded.extraction_meta.get("model_used") == "claude-sonnet-4-6"

        lines_result = await session.execute(
            select(SupplierStatementLine).where(
                SupplierStatementLine.statement_id == stmt_id
            )
        )
        lines = lines_result.scalars().all()
        assert len(lines) >= 1


@pytest.mark.asyncio
async def test_ingest_dismissed_when_no_supplier_name():
    """AP/AR gate: missing supplier_name → DISMISSED."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9002

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(
            return_value=_llm_response(supplier_name=None)
        )),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()

    assert stmt.status == StatementStatus.DISMISSED.value
    assert stmt.extraction_meta.get("dismissed_reason") is not None


@pytest.mark.asyncio
async def test_ingest_dismissed_when_negative_closing_balance():
    """AP/AR gate: negative closing_balance → DISMISSED (AR statement, not AP)."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9003

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(
            return_value=_llm_response(closing_balance=-500.00)
        )),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()

    assert stmt.status == StatementStatus.DISMISSED.value


@pytest.mark.asyncio
async def test_ingest_needs_review_and_escalates_on_balance_mismatch():
    """Balance gate: discrepancy > 0.01 → NEEDS_REVIEW, opus escalation called."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9004

    # Primary response: lines sum to 1000 but closing_balance is 1100 (discrepancy)
    primary_response = json.dumps({
        "supplier_name": "Acme Bearings Pty Ltd",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 1100.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-1", "description": None, "amount": 1000.00},
        ],
    })
    # Escalation response: same discrepancy (opus also can't reconcile)
    escalation_response = json.dumps({
        "supplier_name": "Acme Bearings Pty Ltd",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 1100.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-1", "description": None, "amount": 1000.00},
        ],
    })

    call_count = []

    async def _mock_llm(*args, **kwargs):
        call_count.append(kwargs.get("model", ""))
        if len(call_count) == 1:
            return primary_response
        return escalation_response

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=_mock_llm),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()

    assert stmt.status == StatementStatus.NEEDS_REVIEW.value
    # Escalation model was called
    assert any(settings.statement_llm_model_escalation in m for m in call_count), (
        f"Escalation model not called; calls: {call_count}"
    )
    assert stmt.extraction_meta.get("escalated") is True


@pytest.mark.asyncio
async def test_ingest_escalation_resolves_needs_review():
    """If escalation parse reconciles the balance, status is not NEEDS_REVIEW."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9005

    # Primary: discrepant
    primary_response = json.dumps({
        "supplier_name": "Acme Bearings Pty Ltd",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 1100.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-1", "description": None, "amount": 1000.00},
        ],
    })
    # Escalation: reconciled (lines sum matches closing)
    escalation_response = json.dumps({
        "supplier_name": "Acme Bearings Pty Ltd",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 1100.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-10", "type": "IN", "reference": "INV-1", "description": None, "amount": 1100.00},
        ],
    })

    call_count = [0]

    async def _mock_llm(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return primary_response
        return escalation_response

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=_mock_llm),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()

    # After escalation resolved the discrepancy, status should NOT be NEEDS_REVIEW
    assert stmt.status != StatementStatus.NEEDS_REVIEW.value
    assert stmt.extraction_meta.get("escalation_resolved") is True


@pytest.mark.asyncio
async def test_ingest_idempotent_re_ingest_updates_in_place():
    """Re-ingesting the same source_document_id updates existing row, no duplicate."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9006

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_llm_response())),
    ):
        # First ingest
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt1 = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()
            first_id = stmt1.id

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_llm_response())),
    ):
        # Second ingest (same doc_id)
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt2 = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()
            second_id = stmt2.id

    # Same DB row was updated, not duplicated
    assert first_id == second_id

    # Confirm only one statement in DB for this doc_id
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = _TENANT
        rows = (
            await session.execute(
                select(SupplierStatement).where(
                    SupplierStatement.tenant_id == _TENANT,
                    SupplierStatement.source_document_id == doc_id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_ingest_resolves_reconciled_when_clean():
    """When balance ties and no open exceptions, status is RECONCILED."""
    company_id = await _seed_company_id()
    settings = _fake_settings()
    doc_id = 9007

    # Create a contact + bill in the DB that will match the statement
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = _TENANT
        session.info["company_id"] = company_id

        contact = Contact(
            tenant_id=_TENANT,
            company_id=company_id,
            name="Motion Australia Pty Ltd",
            contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.flush()

        from saebooks.models.account import Account
        from sqlalchemy import text

        # Get an account to attach the bill to
        acct = (await session.execute(
            select(Account).where(Account.company_id == company_id).limit(1)
        )).scalars().first()

        if acct is not None:
            bill = Bill(
                tenant_id=_TENANT,
                company_id=company_id,
                contact_id=contact.id,
                supplier_reference="INV-1001",
                issue_date=date(2026, 5, 10),
                due_date=date(2026, 6, 10),
                status=BillStatus.POSTED,
                subtotal=Decimal("1000.00"),
                tax_total=Decimal("100.00"),
                total=Decimal("1100.00"),
                amount_paid=Decimal("0.00"),
            )
            session.add(bill)
        await session.commit()

    with (
        patch("saebooks.services.statements.ingest.PaperlessClient", _FakePaperlessClient),
        patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_llm_response(
            closing_balance=1100.00,
            lines=[{"date": "2026-05-10", "type": "IN", "reference": "INV-1001",
                    "description": "Bearings", "amount": 1100.00}]
        ))),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )
            await session.commit()

    assert stmt.status == StatementStatus.RECONCILED.value
    assert stmt.our_ap_as_at is not None
    assert stmt.balance_delta is not None
