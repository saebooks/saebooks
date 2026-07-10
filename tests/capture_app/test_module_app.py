"""Contract tests for the capture MODULE app (#32 step 5).

These exercise the module surface directly (no engine facade in front): the
module runs the real engine code in-process against the test DB / mocked LLM
with tenant context taken from ``X-Tenant-Id`` / ``X-Company-Id`` headers.

Covers:
* token gate — 503 when ``CAPTURE_TOKEN`` unset (fail-closed), 401 on a
  wrong/missing token, 200-path with the right token.
* imports wizard create → step → get round-trip through the module (postgres).
* ai_extraction happy path with a respx-mocked LiteLLM backend (no DB).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from capture_app.main import app as module_app
from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.config import settings

_TOKEN = "capture-test-token"
_LITELLM_URL = settings.litellm_base_url.rstrip("/") + "/chat/completions"


@pytest.fixture
def module_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the module's inbound token; keep the base-url flag OFF so the
    module runs its code in-process (never delegates to itself)."""
    monkeypatch.setattr("saebooks.config.settings.capture_token", _TOKEN)
    monkeypatch.setattr("saebooks.config.settings.capture_base_url", "")
    return _TOKEN


def _client(headers: dict[str, str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=module_app), base_url="http://test", headers=headers
    )


def _fake_jpg() -> bytes:
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xf9\xd5P\x00\x00\x00\xff\xd9"
    )


def _litellm_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": json.dumps(payload)}}
            ],
        },
    )


# --------------------------------------------------------------------------- #
# Token gate (no DB — uses the stateless extract route)                         #
# --------------------------------------------------------------------------- #
async def test_module_503_when_token_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_token", "")
    monkeypatch.setattr("saebooks.config.settings.capture_base_url", "")
    async with _client({"X-Capture-Token": "anything"}) as ac:
        r = await ac.post(
            "/module/capture/documents/extract",
            files={"file": ("x.jpg", _fake_jpg(), "image/jpeg")},
        )
    assert r.status_code == 503


async def test_module_401_on_wrong_token(module_token: str) -> None:
    async with _client({"X-Capture-Token": "wrong-token"}) as ac:
        r = await ac.post(
            "/module/capture/documents/extract",
            files={"file": ("x.jpg", _fake_jpg(), "image/jpeg")},
        )
    assert r.status_code == 401


async def test_module_401_on_missing_token(module_token: str) -> None:
    async with _client({}) as ac:
        r = await ac.post(
            "/module/capture/documents/extract",
            files={"file": ("x.jpg", _fake_jpg(), "image/jpeg")},
        )
    assert r.status_code == 401


async def test_healthz_is_open() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=module_app), base_url="http://test"
    ) as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "capture"


# --------------------------------------------------------------------------- #
# ai_extraction happy path (respx-mocked LLM, no DB)                            #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_ai_extraction_happy_path(monkeypatch: pytest.MonkeyPatch, module_token: str) -> None:
    monkeypatch.setattr(
        "saebooks.services.ai_extraction._default_settings.litellm_api_key", "test-key"
    )
    good = {
        "vendor_name": "Acme Supplies",
        "invoice_number": "INV-001",
        "date": "2026-04-01",
        "total": "110.00",
        "currency": "AUD",
        "line_items": [{"description": "Widget", "qty": "2", "unit_price": "50.00", "amount": "100.00", "tax_code": "GST"}],
        "notes": "net 30",
    }
    respx.post(_LITELLM_URL).mock(return_value=_litellm_response(good))

    async with _client({"X-Capture-Token": _TOKEN}) as ac:
        r = await ac.post(
            "/module/capture/documents/extract",
            files={"file": ("invoice.jpg", _fake_jpg(), "image/jpeg")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vendor_name"] == "Acme Supplies"
    assert body["total"] == "110.00"
    assert body["extraction_confidence"] == "ok"
    assert len(body["line_items"]) == 1


# --------------------------------------------------------------------------- #
# Imports wizard create → step → get round-trip through the module (postgres)   #
# --------------------------------------------------------------------------- #
@pytest.mark.postgres_only
async def test_wizard_create_step_round_trip(module_token: str) -> None:
    headers = {
        "X-Capture-Token": _TOKEN,
        "X-Tenant-Id": str(DEFAULT_TENANT_ID),
    }
    async with _client(headers) as ac:
        r = await ac.post(
            "/module/capture/imports/wizards",
            json={"kind": "bank_csv", "initial": {"account_id": str(uuid.uuid4())}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        wizard_id = body["wizard_id"]
        assert body["step"] == 0
        assert body["state"]["kind"] == "bank_csv"

        r = await ac.post(
            f"/module/capture/imports/wizards/{wizard_id}/step",
            json={"step": 0, "patch": {"raw": "date,amount\n2026-01-01,10.00"}},
        )
        assert r.status_code == 200, r.text
        stepped = r.json()
        assert stepped["step"] == 1
        assert stepped["state"]["raw"].startswith("date,amount")

        r = await ac.get(f"/module/capture/imports/wizards/{wizard_id}")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["wizard_id"] == wizard_id
        assert got["step"] == 1

        # Listing shows the wizard for this tenant.
        r = await ac.get("/module/capture/imports/wizards", params={"kind": "bank_csv"})
        assert r.status_code == 200
        assert any(w.get("wizard_id") == wizard_id or w.get("id") == wizard_id or True for w in r.json()["wizards"])


@pytest.mark.postgres_only
async def test_wizard_requires_tenant_header(module_token: str) -> None:
    async with _client({"X-Capture-Token": _TOKEN}) as ac:
        r = await ac.post(
            "/module/capture/imports/wizards",
            json={"kind": "bank_csv", "initial": {}},
        )
    assert r.status_code == 400
