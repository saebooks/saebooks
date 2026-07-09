"""Tests for services/statements/extract.py.

The LLM call is monkeypatched so these run entirely in-process without
hitting a real litellm gateway.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from saebooks.config import Settings
from saebooks.services.statements import extract as extract_mod
from saebooks.services.statements.extract import (
    ExtractedStatement,
    extract_statement,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://x:x@db:5432/x",
        STATEMENT_LLM_BASE="http://litellm:4000/v1",
        STATEMENT_LLM_MODEL="claude-sonnet-4-6",
        STATEMENT_LLM_MODEL_ESCALATION="claude-opus-4-7",
        STATEMENT_LLM_API_KEY="test-key",
    )


_SAMPLE_LLM_RESPONSE = json.dumps({
    "supplier_name": "Motion Australia Pty Ltd",
    "supplier_abn": "32 000 143 608",
    "customer_ref": "SAE-0042",
    "statement_date": "2026-05-31",
    "terms": "30 Days",
    "closing_balance": 3300.00,
    "opening_balance": 0.00,
    "lines": [
        {
            "date": "2026-05-10",
            "type": "IN",
            "reference": "INV-1001",
            "description": "Bearings",
            "amount": 1100.00,
        },
        {
            "date": "2026-05-15",
            "type": "IN",
            "reference": "INV-1002",
            "description": "Seals",
            "amount": 2200.00,
        },
        {
            "date": "2026-05-20",
            "type": "PY",
            "reference": "PY-001",
            "description": "Payment received",
            "amount": -500.00,
        },
    ],
})

_SAMPLE_OCR = "Motion Australia Pty Ltd\nABN: 32 000 143 608\n..."


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_parses_sample_ocr(test_settings):
    """extract_statement maps LLM JSON response to ExtractedStatement fields."""
    mock_response = _SAMPLE_LLM_RESPONSE

    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=mock_response)):
        result = await extract_statement(_SAMPLE_OCR, settings=test_settings)

    assert isinstance(result, ExtractedStatement)
    assert result.supplier_name == "Motion Australia Pty Ltd"
    assert result.supplier_abn == "32 000 143 608"
    assert result.customer_ref == "SAE-0042"
    assert result.statement_date == date(2026, 5, 31)
    assert result.terms == "30 Days"
    assert result.closing_balance == Decimal("3300.00")
    assert result.opening_balance == Decimal("0.00")
    assert len(result.lines) == 3
    assert result.model_used == "claude-sonnet-4-6"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_extract_invoice_lines_positive(test_settings):
    """Invoice lines have positive amounts; payment lines are forced negative."""
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_SAMPLE_LLM_RESPONSE)):
        result = await extract_statement(_SAMPLE_OCR, settings=test_settings)

    invoices = [l for l in result.lines if l.line_type == "invoice"]
    payments = [l for l in result.lines if l.line_type == "payment"]

    assert all(l.amount > 0 for l in invoices)
    assert all(l.amount < 0 for l in payments)


@pytest.mark.asyncio
async def test_extract_payment_positive_forced_negative(test_settings):
    """A payment with a positive amount in LLM response is forced to negative."""
    resp = json.dumps({
        "supplier_name": "Acme Pty Ltd",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 500.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-01", "type": "PY", "reference": "P1", "description": None, "amount": 200.00},
        ],
    })
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=resp)):
        result = await extract_statement("ocr...", settings=test_settings)

    assert result.lines[0].amount == Decimal("-200.00")


@pytest.mark.asyncio
async def test_extract_model_override_sets_escalated(test_settings):
    """Passing model_override=escalation_model sets escalated=True on result."""
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=_SAMPLE_LLM_RESPONSE)):
        result = await extract_statement(
            _SAMPLE_OCR,
            settings=test_settings,
            model_override=test_settings.statement_llm_model_escalation,
        )

    assert result.escalated is True
    assert result.model_used == test_settings.statement_llm_model_escalation


@pytest.mark.asyncio
async def test_extract_strips_markdown_fences(test_settings):
    """Markdown code fences around JSON are stripped before parsing."""
    fenced = f"```json\n{_SAMPLE_LLM_RESPONSE}\n```"
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=fenced)):
        result = await extract_statement(_SAMPLE_OCR, settings=test_settings)

    assert result.supplier_name == "Motion Australia Pty Ltd"


@pytest.mark.asyncio
async def test_extract_derives_closing_balance_if_missing(test_settings):
    """If closing_balance is null, it is derived as the sum of line amounts."""
    resp = json.dumps({
        "supplier_name": "Widget Co",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": None,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-01", "type": "IN", "reference": "I1", "description": None, "amount": 110.00},
            {"date": "2026-05-02", "type": "IN", "reference": "I2", "description": None, "amount": 220.00},
        ],
    })
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=resp)):
        result = await extract_statement("ocr...", settings=test_settings)

    assert result.closing_balance == Decimal("330.00")


@pytest.mark.asyncio
async def test_extract_skips_unparseable_lines(test_settings):
    """Lines with unparseable amounts are skipped, not raised."""
    resp = json.dumps({
        "supplier_name": "Widget Co",
        "supplier_abn": None,
        "customer_ref": None,
        "statement_date": "2026-05-31",
        "terms": None,
        "closing_balance": 110.00,
        "opening_balance": None,
        "lines": [
            {"date": "2026-05-01", "type": "IN", "reference": "I1", "description": None, "amount": 110.00},
            {"date": "2026-05-02", "type": "IN", "reference": "I2", "description": None, "amount": None},
        ],
    })
    with patch.object(extract_mod, "_call_llm", new=AsyncMock(return_value=resp)):
        result = await extract_statement("ocr...", settings=test_settings)

    assert len(result.lines) == 1
