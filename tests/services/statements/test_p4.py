"""Tests for P4 deliverables: per-supplier template hint + vision fallback.

D1: per-supplier template hint injected into LLM system prompt.
D2: vision fallback when OCR is absent / too short.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from saebooks.config import Settings
from saebooks.services.statements import extract as extract_mod
from saebooks.services.statements.extract import (
    _SYSTEM_PROMPT,
    _build_system_prompt,
    extract_statement,
    extract_statement_vision,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://x:x@db:5432/x",
        STATEMENT_LLM_BASE="http://litellm:4000/v1",
        STATEMENT_LLM_MODEL="claude-sonnet-4-6",
        STATEMENT_LLM_MODEL_ESCALATION="claude-opus-4-7",
        STATEMENT_LLM_API_KEY="test-key",
        STATEMENT_LLM_VISION_MODEL="claude-haiku-4-5",
    )


_MINIMAL_LLM_RESPONSE = json.dumps({
    "supplier_name": "Test Supplier Pty Ltd",
    "supplier_abn": "12 345 678 901",
    "customer_ref": "ACCT-99",
    "statement_date": "2026-05-31",
    "terms": "30 Days",
    "closing_balance": 550.00,
    "opening_balance": 0.00,
    "lines": [
        {"date": "2026-05-01", "type": "IN", "reference": "INV-X",
         "description": "Widgets", "amount": 550.00},
    ],
})


# ===========================================================================
# D1: Template hint — extract_statement
# ===========================================================================

class TestTemplateHint:
    """D1: prompt_hint is appended to the system prompt; base prompt unchanged."""

    @pytest.mark.asyncio
    async def test_no_hint_uses_base_system_prompt(self, test_settings):
        """When no prompt_hint is given, _call_llm receives the unmodified _SYSTEM_PROMPT."""
        captured: list[str] = []

        async def _capture(system, user, *, model, base_url, api_key):
            captured.append(system)
            return _MINIMAL_LLM_RESPONSE

        with patch.object(extract_mod, "_call_llm", new=_capture):
            await extract_statement("ocr text", settings=test_settings)

        assert len(captured) == 1
        assert captured[0] == _SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_hint_appended_to_system_prompt(self, test_settings):
        """When prompt_hint is set, the system prompt contains both the base
        prompt and the supplier-specific hint block."""
        hint = "Column 4 is the invoice total; ignore column 3 (GST only)."
        captured: list[str] = []

        async def _capture(system, user, *, model, base_url, api_key):
            captured.append(system)
            return _MINIMAL_LLM_RESPONSE

        with patch.object(extract_mod, "_call_llm", new=_capture):
            await extract_statement("ocr text", settings=test_settings, prompt_hint=hint)

        assert len(captured) == 1
        system = captured[0]
        # Base prompt is preserved
        assert _SYSTEM_PROMPT in system
        # Hint block is appended
        assert "Supplier-specific extraction guidance" in system
        assert hint in system

    @pytest.mark.asyncio
    async def test_hint_does_not_replace_base_prompt(self, test_settings):
        """The hint is appended; the original base system prompt text is still present."""
        hint = "Look only at page 1."
        captured: list[str] = []

        async def _capture(system, user, *, model, base_url, api_key):
            captured.append(system)
            return _MINIMAL_LLM_RESPONSE

        with patch.object(extract_mod, "_call_llm", new=_capture):
            await extract_statement("ocr text", settings=test_settings, prompt_hint=hint)

        # Key line from the base prompt
        assert "Return ONLY the JSON object" in captured[0]

    def test_build_system_prompt_no_hint(self):
        """_build_system_prompt with no hint returns exact _SYSTEM_PROMPT."""
        assert _build_system_prompt(None) is _SYSTEM_PROMPT
        assert _build_system_prompt("") == _SYSTEM_PROMPT

    def test_build_system_prompt_with_hint(self):
        """_build_system_prompt with hint appends the guidance block."""
        hint = "Amounts are in column 5."
        result = _build_system_prompt(hint)
        assert result.startswith(_SYSTEM_PROMPT)
        assert "Supplier-specific extraction guidance" in result
        assert hint in result


# ===========================================================================
# D2: Vision fallback — extract_statement_vision
# ===========================================================================

class TestVisionFallback:
    """D2: extract_statement_vision uses _call_llm_vision and reuses _build_extracted_statement."""

    @pytest.mark.asyncio
    async def test_vision_path_called_with_image(self, test_settings):
        """extract_statement_vision posts to _call_llm_vision with correct model."""
        captured: list[dict] = []

        async def _capture_vision(system, img, mime, *, model, base_url, api_key):
            captured.append({"system": system, "mime": mime, "model": model})
            return _MINIMAL_LLM_RESPONSE

        with patch.object(extract_mod, "_call_llm_vision", new=_capture_vision):
            result = await extract_statement_vision(
                b"fake-image-bytes",
                "image/jpeg",
                settings=test_settings,
            )

        assert len(captured) == 1
        assert captured[0]["model"] == "claude-haiku-4-5"
        assert captured[0]["mime"] == "image/jpeg"
        assert result.supplier_name == "Test Supplier Pty Ltd"

    @pytest.mark.asyncio
    async def test_vision_result_parsed_correctly(self, test_settings):
        """extract_statement_vision returns a valid ExtractedStatement."""
        with patch.object(
            extract_mod, "_call_llm_vision",
            new=AsyncMock(return_value=_MINIMAL_LLM_RESPONSE),
        ):
            result = await extract_statement_vision(
                b"bytes",
                "image/png",
                settings=test_settings,
            )

        assert result.supplier_name == "Test Supplier Pty Ltd"
        assert result.closing_balance == Decimal("550.00")
        assert len(result.lines) == 1
        assert result.model_used == "claude-haiku-4-5"
        assert result.escalated is False

    @pytest.mark.asyncio
    async def test_vision_hint_appended_to_system(self, test_settings):
        """Vision path also injects prompt_hint into the system prompt."""
        hint = "Page 1 summary block only — ignore detail pages."
        captured: list[str] = []

        async def _capture_vision(system, img, mime, *, model, base_url, api_key):
            captured.append(system)
            return _MINIMAL_LLM_RESPONSE

        with patch.object(extract_mod, "_call_llm_vision", new=_capture_vision):
            await extract_statement_vision(
                b"bytes",
                "image/jpeg",
                settings=test_settings,
                prompt_hint=hint,
            )

        assert hint in captured[0]
        assert "Supplier-specific extraction guidance" in captured[0]


# ===========================================================================
# D2: Vision fallback trigger in ingest_statement
# ===========================================================================

pytestmark_pg = pytest.mark.postgres_only

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _fake_settings_ingest() -> Settings:
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
        STATEMENT_LLM_VISION_MODEL="claude-haiku-4-5",
        SAEBOOKS_SQL_RO_PASSWORD="saebooks_sql_ro_test_pw",
    )


class _FakePaperlessClientNoOCR:
    """Paperless client that returns empty OCR content + image bytes."""
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def get_document(self, document_id: int) -> dict:
        return {
            "id": document_id,
            "title": f"Statement {document_id}.pdf",
            "content": "",  # empty OCR
        }

    async def download_content(self, document_id: int) -> tuple[bytes, str | None]:
        return b"fake-image-bytes", "image/jpeg"


class _FakePaperlessClientShortOCR:
    """Paperless client that returns a very short OCR text (below _MIN_OCR_CHARS)."""
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def get_document(self, document_id: int) -> dict:
        return {
            "id": document_id,
            "title": f"Statement {document_id}.pdf",
            "content": "short",  # 5 chars < 40
        }

    async def download_content(self, document_id: int) -> tuple[bytes, str | None]:
        return b"fake-image-bytes", "image/png"


class _FakePaperlessClientWithOCR:
    """Paperless client that returns sufficient OCR text."""
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def get_document(self, document_id: int) -> dict:
        return {
            "id": document_id,
            "title": f"Statement {document_id}.pdf",
            "content": "Motion Australia Pty Ltd\nABN: 83 914 571 673\nStatement Date: 31/05/2026\n...",
        }

    async def download_content(self, document_id: int) -> tuple[bytes, str | None]:
        # Should not be called when OCR is present
        raise AssertionError("download_content called when OCR text is present")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_ingest_uses_vision_when_ocr_empty():
    """When OCR is empty, ingest calls download_content + vision path;
    extraction_meta['vision'] is True."""
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company

    settings = _fake_settings_ingest()

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).limit(1)
        )).scalars().first()
        assert co is not None
        company_id = co.id

    from saebooks.services.statements import extract as extract_mod_inner
    from saebooks.services.statements.ingest import ingest_statement

    doc_id = 9100

    with (
        patch(
            "saebooks.services.statements.ingest.PaperlessClient",
            _FakePaperlessClientNoOCR,
        ),
        patch.object(
            extract_mod_inner,
            "_call_llm_vision",
            new=AsyncMock(return_value=_MINIMAL_LLM_RESPONSE),
        ),
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

    assert stmt.extraction_meta.get("vision") is True
    assert stmt.supplier_name == "Test Supplier Pty Ltd"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_ingest_uses_vision_when_ocr_short():
    """When OCR is fewer than 40 chars, ingest uses vision fallback."""
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company

    settings = _fake_settings_ingest()

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).limit(1)
        )).scalars().first()
        assert co is not None
        company_id = co.id

    from saebooks.services.statements import extract as extract_mod_inner
    from saebooks.services.statements.ingest import ingest_statement

    doc_id = 9101

    with (
        patch(
            "saebooks.services.statements.ingest.PaperlessClient",
            _FakePaperlessClientShortOCR,
        ),
        patch.object(
            extract_mod_inner,
            "_call_llm_vision",
            new=AsyncMock(return_value=_MINIMAL_LLM_RESPONSE),
        ),
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

    assert stmt.extraction_meta.get("vision") is True


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_ingest_does_not_use_vision_when_ocr_present():
    """When OCR text is >= 40 chars, vision path is NOT used; download_content
    is never called."""
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company

    settings = _fake_settings_ingest()

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).limit(1)
        )).scalars().first()
        assert co is not None
        company_id = co.id

    from saebooks.services.statements import extract as extract_mod_inner
    from saebooks.services.statements.ingest import ingest_statement

    doc_id = 9102

    with (
        patch(
            "saebooks.services.statements.ingest.PaperlessClient",
            _FakePaperlessClientWithOCR,
        ),
        patch.object(
            extract_mod_inner,
            "_call_llm",
            new=AsyncMock(return_value=_MINIMAL_LLM_RESPONSE),
        ),
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

    # vision key should not be set (or should be absent / falsy)
    assert not stmt.extraction_meta.get("vision"), stmt.extraction_meta


# ===========================================================================
# D1: Template lookup + extraction_meta["template_id"] — ingest integration
# ===========================================================================

@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_ingest_applies_template_hint_on_reingest():
    """On re-ingest, if an active SupplierStatementTemplate exists for the
    resolved contact_id, its prompt_hint is appended to the system prompt
    and extraction_meta['template_id'] is set."""
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company
    from saebooks.models.contact import Contact, ContactType
    from saebooks.models.supplier_statement_template import SupplierStatementTemplate
    from saebooks.services.statements import extract as extract_mod_inner
    from saebooks.services.statements.ingest import ingest_statement

    settings = _fake_settings_ingest()

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).limit(1)
        )).scalars().first()
        assert co is not None
        company_id = co.id

    HINT_TEXT = "Use column 4 for invoice total; ignore column 3 (tax only)."
    contact_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    doc_id = 9110

    # Create contact + template
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        s.info["company_id"] = company_id
        contact = Contact(
            tenant_id=_TENANT,
            company_id=company_id,
            name="Test Supplier Pty Ltd",
            contact_type=ContactType.SUPPLIER,
        )
        s.add(contact)
        await s.flush()
        contact_id = contact.id

        tmpl = SupplierStatementTemplate(
            tenant_id=_TENANT,
            company_id=company_id,
            contact_id=contact_id,
            supplier_name="Test Supplier Pty Ltd",
            prompt_hint=HINT_TEXT,
            active=True,
        )
        s.add(tmpl)
        await s.flush()
        template_id = tmpl.id
        await s.commit()

    # First ingest — no template lookup (no existing_stmt)
    with (
        patch(
            "saebooks.services.statements.ingest.PaperlessClient",
            _FakePaperlessClientWithOCR,
        ),
        patch.object(
            extract_mod_inner,
            "_call_llm",
            new=AsyncMock(return_value=_MINIMAL_LLM_RESPONSE),
        ),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt1 = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )

    # First pass: no template_id in meta (no existing statement to match against)
    assert "template_id" not in stmt1.extraction_meta

    # Second ingest (re-ingest) — existing_stmt now present with contact_id resolved
    captured_systems: list[str] = []

    async def _capture_llm(system, user, *, model, base_url, api_key):
        captured_systems.append(system)
        return _MINIMAL_LLM_RESPONSE

    with (
        patch(
            "saebooks.services.statements.ingest.PaperlessClient",
            _FakePaperlessClientWithOCR,
        ),
        patch.object(extract_mod_inner, "_call_llm", new=_capture_llm),
    ):
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = _TENANT
            stmt2 = await ingest_statement(
                session,
                tenant_id=_TENANT,
                company_id=company_id,
                paperless_document_id=doc_id,
                settings=settings,
            )

    # Re-ingest: template should have been found via contact_id
    assert stmt2.extraction_meta.get("template_id") == str(template_id), (
        f"expected template_id={template_id}, got meta={stmt2.extraction_meta}"
    )
    # Hint must appear in the system prompt the LLM saw
    assert any(HINT_TEXT in s for s in captured_systems), (
        f"hint not found in captured system prompts: {captured_systems}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_template_contact_id_wins_over_abn():
    """contact_id match takes priority over supplier_abn match.

    Hermetic fix: the shared "first active company" picked here is not
    guaranteed to belong to the hardcoded ``_TENANT`` — under full-suite
    ordering the seed company for tenant ``000...001`` may not sort first,
    and the query has no tenant filter. Inserting a ``Contact`` with
    ``tenant_id=_TENANT`` against a company whose REAL tenant_id differs
    trips the ``tenant_coherence`` trigger. Read the company's actual
    ``tenant_id`` and use it consistently instead of assuming ``_TENANT``
    — same pattern as ``tests/services/test_business_identifiers.py``'s
    hermetic fix.
    """
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company
    from saebooks.models.contact import Contact, ContactType
    from saebooks.models.supplier_statement_template import SupplierStatementTemplate
    from saebooks.services.statements.ingest import _lookup_template

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (await s.execute(
            select(Company).where(Company.archived_at.is_(None)).limit(1)
        )).scalars().first()
        assert co is not None
        company_id = co.id
        tenant_id = co.tenant_id

    abn = "99 888 777 666"

    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = tenant_id
        s.info["company_id"] = company_id

        contact = Contact(
            tenant_id=tenant_id,
            company_id=company_id,
            name="Priority Test Supplier",
            contact_type=ContactType.SUPPLIER,
        )
        s.add(contact)
        await s.flush()

        # Template matched by contact_id (highest priority)
        tmpl_contact = SupplierStatementTemplate(
            tenant_id=tenant_id,
            company_id=company_id,
            contact_id=contact.id,
            supplier_abn=abn,
            prompt_hint="Contact-level hint.",
            active=True,
        )
        s.add(tmpl_contact)

        # Template matched by ABN only (lower priority)
        tmpl_abn = SupplierStatementTemplate(
            tenant_id=tenant_id,
            company_id=company_id,
            contact_id=None,
            supplier_abn=abn,
            prompt_hint="ABN-level hint.",
            active=True,
        )
        s.add(tmpl_abn)
        await s.flush()
        contact_tmpl_id = tmpl_contact.id
        await s.commit()

    # Lookup with contact_id set should return the contact-level template
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = tenant_id
        s.info["company_id"] = company_id
        result = await _lookup_template(
            s,
            company_id=company_id,
            contact_id=contact.id,
            supplier_abn=abn,
            supplier_name="Priority Test Supplier",
        )

    assert result is not None
    assert result.id == contact_tmpl_id
    assert result.prompt_hint == "Contact-level hint."
