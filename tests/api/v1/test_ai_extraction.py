"""Tests for ``POST /api/v1/documents/extract`` (B/46 AI extraction).

Coverage:
* Feature flag gate — community edition → 404
* Offline edition → 404
* Business edition + JPEG upload → 200 with extraction result
* Business edition + PDF upload → 200 with extraction result
* Business edition + PNG upload → 200 with extraction result
* Unsupported MIME type → 422
* Anthropic API error → 200 with partial result + extraction_error field
* Missing bearer token → 401
* extraction_confidence is "ok" on success, "partial" on API error
* AiExtractionNotConfiguredError (missing API key) → 503
"""
from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app

# ---------------------------------------------------------------------- #
# Fixtures                                                                #
# ---------------------------------------------------------------------- #


@pytest.fixture
def bearer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_token()}"}


@pytest.fixture
async def business_client(
    monkeypatch: pytest.MonkeyPatch,
    bearer_headers: dict[str, str],
) -> AsyncClient:
    """Authenticated client with Business edition active."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


@pytest.fixture
async def community_client(
    monkeypatch: pytest.MonkeyPatch,
    bearer_headers: dict[str, str],
) -> AsyncClient:
    """Authenticated client with Community edition active."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "community")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


@pytest.fixture
async def offline_client(
    monkeypatch: pytest.MonkeyPatch,
    bearer_headers: dict[str, str],
) -> AsyncClient:
    """Authenticated client with Offline edition active."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "offline")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=bearer_headers,
    ) as ac:
        yield ac


def _fake_jpg() -> bytes:
    """1x1 pixel JPEG bytes — minimal valid JPEG for upload tests."""
    # Minimal valid JPEG (white 1×1 pixel)
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xf9\xd5P\x00\x00\x00\xff\xd9"
    )


def _fake_png() -> bytes:
    """Minimal valid 1×1 white PNG."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xcc\xd9\x9d"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _fake_pdf() -> bytes:
    """Minimal well-formed PDF bytes."""
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"


def _make_anthropic_response(payload: dict[str, Any]) -> MagicMock:
    """Build a mock that looks like an ``anthropic.types.Message``."""
    content_block = MagicMock()
    content_block.text = json.dumps(payload)
    msg = MagicMock()
    msg.content = [content_block]
    return msg


_GOOD_EXTRACTION = {
    "vendor_name": "Acme Supplies",
    "invoice_number": "INV-001",
    "date": "2026-04-01",
    "due_date": "2026-04-30",
    "subtotal": "100.00",
    "tax_amount": "10.00",
    "total": "110.00",
    "currency": "AUD",
    "line_items": [
        {
            "description": "Widget",
            "qty": "2",
            "unit_price": "50.00",
            "amount": "100.00",
            "tax_code": "GST",
        }
    ],
    "notes": "Payment due 30 days",
}

# ---------------------------------------------------------------------- #
# Feature gate tests                                                      #
# ---------------------------------------------------------------------- #


async def test_community_edition_returns_404(community_client: AsyncClient) -> None:
    """FLAG_AI_EXTRACTION is not available on Community — endpoint must 404."""
    resp = await community_client.post(
        "/api/v1/documents/extract",
        files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
    )
    assert resp.status_code == 404


async def test_offline_edition_returns_404(offline_client: AsyncClient) -> None:
    """FLAG_AI_EXTRACTION is not available on Offline — endpoint must 404."""
    resp = await offline_client.post(
        "/api/v1/documents/extract",
        files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------- #
# Auth gate                                                               #
# ---------------------------------------------------------------------- #


async def test_missing_bearer_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Authorization header → 401, even on Business edition."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/api/v1/documents/extract",
            files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------- #
# Successful extraction — JPEG                                            #
# ---------------------------------------------------------------------- #


async def test_jpeg_upload_returns_extraction(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JPEG upload on Business tier → 200 with structured extraction data."""
    mock_response = _make_anthropic_response(_GOOD_EXTRACTION)

    mock_create = AsyncMock(return_value=mock_response)
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client_instance = MagicMock()
    mock_client_instance.messages = mock_messages

    with patch(
        "saebooks.services.ai_extraction.anthropic.AsyncAnthropic",
        return_value=mock_client_instance,
    ):
        monkeypatch.setattr(
            "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
            "test-key",
        )
        resp = await business_client.post(
            "/api/v1/documents/extract",
            files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["vendor_name"] == "Acme Supplies"
    assert body["invoice_number"] == "INV-001"
    assert body["total"] == "110.00"
    assert body["currency"] == "AUD"
    assert len(body["line_items"]) == 1
    assert body["line_items"][0]["description"] == "Widget"
    assert body["extraction_confidence"] == "ok"
    assert body["extraction_error"] is None


# ---------------------------------------------------------------------- #
# Successful extraction — PDF                                             #
# ---------------------------------------------------------------------- #


async def test_pdf_upload_returns_extraction(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF upload on Business tier → 200 with structured extraction data."""
    mock_response = _make_anthropic_response(_GOOD_EXTRACTION)

    mock_create = AsyncMock(return_value=mock_response)
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client_instance = MagicMock()
    mock_client_instance.messages = mock_messages

    with patch(
        "saebooks.services.ai_extraction.anthropic.AsyncAnthropic",
        return_value=mock_client_instance,
    ):
        monkeypatch.setattr(
            "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
            "test-key",
        )
        resp = await business_client.post(
            "/api/v1/documents/extract",
            files={"file": ("statement.pdf", io.BytesIO(_fake_pdf()), "application/pdf")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["vendor_name"] == "Acme Supplies"
    assert body["extraction_confidence"] == "ok"


# ---------------------------------------------------------------------- #
# Successful extraction — PNG                                             #
# ---------------------------------------------------------------------- #


async def test_png_upload_returns_extraction(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PNG upload → 200."""
    mock_response = _make_anthropic_response(_GOOD_EXTRACTION)

    mock_create = AsyncMock(return_value=mock_response)
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client_instance = MagicMock()
    mock_client_instance.messages = mock_messages

    with patch(
        "saebooks.services.ai_extraction.anthropic.AsyncAnthropic",
        return_value=mock_client_instance,
    ):
        monkeypatch.setattr(
            "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
            "test-key",
        )
        resp = await business_client.post(
            "/api/v1/documents/extract",
            files={"file": ("receipt.png", io.BytesIO(_fake_png()), "image/png")},
        )

    assert resp.status_code == 200
    assert resp.json()["extraction_confidence"] == "ok"


# ---------------------------------------------------------------------- #
# Unsupported MIME type                                                   #
# ---------------------------------------------------------------------- #


async def test_unsupported_mime_type_returns_422(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text/plain upload → 422 with a clear detail message."""
    monkeypatch.setattr(
        "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
        "test-key",
    )
    resp = await business_client.post(
        "/api/v1/documents/extract",
        files={"file": ("invoice.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "text/plain" in detail
    assert "image/jpeg" in detail  # check accepted types are listed


# ---------------------------------------------------------------------- #
# Anthropic API error → graceful degradation                             #
# ---------------------------------------------------------------------- #


async def test_anthropic_api_error_returns_partial_result(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Anthropic API raises, endpoint returns 200 with extraction_error set."""
    mock_create = AsyncMock(side_effect=RuntimeError("connection timeout"))
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client_instance = MagicMock()
    mock_client_instance.messages = mock_messages

    with patch(
        "saebooks.services.ai_extraction.anthropic.AsyncAnthropic",
        return_value=mock_client_instance,
    ):
        monkeypatch.setattr(
            "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
            "test-key",
        )
        resp = await business_client.post(
            "/api/v1/documents/extract",
            files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["extraction_confidence"] == "partial"
    assert body["extraction_error"] is not None
    assert "connection timeout" in body["extraction_error"]
    # Core fields are None on partial result
    assert body["vendor_name"] is None
    assert body["total"] is None
    assert body["line_items"] == []


# ---------------------------------------------------------------------- #
# Missing API key → 503                                                   #
# ---------------------------------------------------------------------- #


async def test_missing_api_key_returns_503(
    business_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ANTHROPIC_API_KEY is empty the service raises; endpoint must return 503."""
    monkeypatch.setattr(
        "saebooks.services.ai_extraction._default_settings.anthropic_api_key",
        "",
    )
    resp = await business_client.post(
        "/api/v1/documents/extract",
        files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
    )
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]
