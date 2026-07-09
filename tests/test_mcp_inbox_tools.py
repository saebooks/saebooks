"""Contract tests for the document-inbox MCP tools (issue #33 phase 4).

Same convention as ``test_mcp_contract_drift.py``: the tool functions
are plain async callables; the HTTP helpers (``_get``/``_post``/
``_patch``/``_client_for``) are monkeypatched to capture exactly what
would go over the wire, and the captured bodies are validated against
the inbox router's own Pydantic request models — the same validation
the real API performs.
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest

from saebooks.api.v1 import document_inbox as inbox_api
from saebooks.mcp import server as mcp_server

_NINE_TOOLS = (
    "list_inbox_documents",
    "get_inbox_document",
    "upload_inbox_document",
    "update_inbox_document",
    "retry_inbox_extraction",
    "publish_inbox_document",
    "reject_inbox_document",
    "list_supplier_rules",
    "create_supplier_rule",
)


def _uuid() -> str:
    return str(uuid.uuid4())


class _Capture:
    def __init__(self) -> None:
        self.method: str | None = None
        self.path: str | None = None
        self.body: dict[str, Any] | None = None
        self.params: dict[str, Any] | None = None
        self.idempotency_key: str | None = None


@pytest.fixture()
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    cap = _Capture()

    async def fake_get(ctx: Any, path: str, **params: Any) -> dict[str, Any]:
        cap.method, cap.path, cap.params = "GET", path, params
        return {"items": [], "total": 0}

    async def fake_post(
        ctx: Any,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        if_match: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        cap.method, cap.path, cap.body = "POST", path, body or {}
        cap.idempotency_key = idempotency_key
        return {"id": _uuid(), "version": 1}

    async def fake_patch(
        ctx: Any, path: str, body: dict[str, Any], *, if_match: Any = None
    ) -> dict[str, Any]:
        cap.method, cap.path, cap.body = "PATCH", path, body
        return {"id": _uuid(), "version": 2}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_patch", fake_patch)
    return cap


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_all_nine_inbox_tools_registered() -> None:
    tools = mcp_server.mcp._tool_manager._tools
    for name in _NINE_TOOLS:
        assert name in tools, f"MCP tool {name} not registered"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_inbox_documents_hits_inbox_route(capture: _Capture) -> None:
    await mcp_server.list_inbox_documents(
        ctx=None, status="NEEDS_REVIEW", source="UPLOAD", page_size=25, page=2
    )
    assert capture.path == "/api/v1/inbox/documents"
    assert capture.params == {
        "status": "NEEDS_REVIEW", "source": "UPLOAD", "company_id": "",
        "page_size": 25, "page": 2,
    }


async def test_get_inbox_document_path(capture: _Capture) -> None:
    doc_id = _uuid()
    await mcp_server.get_inbox_document(ctx=None, document_id=doc_id)
    assert capture.path == f"/api/v1/inbox/documents/{doc_id}"


async def test_list_supplier_rules_path_and_params(capture: _Capture) -> None:
    await mcp_server.list_supplier_rules(ctx=None, include_inactive=True)
    assert capture.path == "/api/v1/inbox/supplier-rules"
    assert capture.params is not None
    assert capture.params["include_inactive"] is True


# ---------------------------------------------------------------------------
# Update — body must satisfy the router's InboxDocumentPatch (extra=forbid)
# ---------------------------------------------------------------------------


async def test_update_inbox_document_body_matches_schema(capture: _Capture) -> None:
    doc_id = _uuid()
    await mcp_server.update_inbox_document(
        ctx=None,
        document_id=doc_id,
        version=3,
        extraction_override={
            "vendor_name": "BP Wacol",
            "invoice_number": "INV-9",
            "total": "110.00",
            "line_items": [
                {
                    "description": "Fuel",
                    "quantity": "1",
                    "unit_price": "110.00",
                    "account_id": _uuid(),
                    "tax_code_id": _uuid(),
                }
            ],
        },
        company_id=_uuid(),
        suggested_contact_id=_uuid(),
    )
    assert capture.method == "PATCH"
    assert capture.path == f"/api/v1/inbox/documents/{doc_id}"
    # The optimistic lock rides in the body (not If-Match) on this route.
    assert capture.body is not None and capture.body["version"] == 3
    inbox_api.InboxDocumentPatch.model_validate(capture.body)


async def test_update_inbox_document_omits_empty_fields(capture: _Capture) -> None:
    await mcp_server.update_inbox_document(
        ctx=None, document_id=_uuid(), version=1,
        extraction_override={"total": "10.00"},
    )
    assert capture.body == {
        "version": 1, "extraction_override": {"total": "10.00"},
    }
    inbox_api.InboxDocumentPatch.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Publish — body vs PublishBody + the self-generated idempotency key
# ---------------------------------------------------------------------------


async def test_publish_inbox_document_body_and_idempotency_key(
    capture: _Capture,
) -> None:
    doc_id = _uuid()
    await mcp_server.publish_inbox_document(
        ctx=None,
        document_id=doc_id,
        version=4,
        record_kind="EXPENSE",
        company_id=_uuid(),
        contact_id=_uuid(),
        date="2026-07-04",
        lines=[
            {
                "description": "Fuel",
                "account_id": _uuid(),
                "tax_code_id": _uuid(),
                "quantity": "1",
                "unit_price": "110.00",
            }
        ],
        payment_account_id=_uuid(),
        reference="INV-9",
        learn_rule=True,
    )
    assert capture.path == f"/api/v1/inbox/documents/{doc_id}/publish"
    # Spec §3: the key is self-generated from (document_id, version) so
    # a retried call replays instead of double-publishing.
    assert capture.idempotency_key == f"mcp-inbox-publish-{doc_id}-v4"
    assert capture.body is not None
    assert capture.body["learn_rule"] is True
    inbox_api.PublishBody.model_validate(capture.body)


async def test_publish_bill_omits_payment_account(capture: _Capture) -> None:
    await mcp_server.publish_inbox_document(
        ctx=None,
        document_id=_uuid(),
        version=1,
        record_kind="BILL",
        company_id=_uuid(),
        contact_id=_uuid(),
        date="2026-07-04",
        lines=[{"description": "Widgets", "account_id": _uuid(),
                "quantity": "2", "unit_price": "50.00"}],
        due_date="2026-08-03",
    )
    assert capture.body is not None
    assert "payment_account_id" not in capture.body
    assert capture.body["due_date"] == "2026-08-03"
    inbox_api.PublishBody.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Retry / reject
# ---------------------------------------------------------------------------


async def test_retry_inbox_extraction_path(capture: _Capture) -> None:
    doc_id = _uuid()
    await mcp_server.retry_inbox_extraction(ctx=None, document_id=doc_id)
    assert capture.method == "POST"
    assert capture.path == f"/api/v1/inbox/documents/{doc_id}/extract"
    assert capture.body == {}


async def test_reject_inbox_document_body_matches_schema(capture: _Capture) -> None:
    doc_id = _uuid()
    await mcp_server.reject_inbox_document(
        ctx=None, document_id=doc_id, reason="NOT_A_DOCUMENT", note="blank page"
    )
    assert capture.path == f"/api/v1/inbox/documents/{doc_id}/reject"
    inbox_api.RejectBody.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Supplier rule create — body vs SupplierRuleCreate (extra=forbid)
# ---------------------------------------------------------------------------


async def test_create_supplier_rule_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_supplier_rule(
        ctx=None,
        vendor_name="BP Wacol",
        contact_id=_uuid(),
        vendor_abn="12345678901",
        account_id=_uuid(),
        tax_code_id=_uuid(),
        record_kind="EXPENSE",
    )
    assert capture.path == "/api/v1/inbox/supplier-rules"
    inbox_api.SupplierRuleCreate.model_validate(capture.body)


async def test_create_supplier_rule_minimal_body(capture: _Capture) -> None:
    await mcp_server.create_supplier_rule(
        ctx=None, vendor_name="BP Wacol", contact_id=_uuid()
    )
    assert capture.body is not None
    assert set(capture.body) == {"vendor_name", "contact_id"}
    inbox_api.SupplierRuleCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Upload — multipart through _client_for (no JSON body variant exists)
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 201
    content = b"{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"id": str(uuid.uuid4()), "duplicate": False}


class _FakeClient:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
        self._record["path"] = path
        self._record.update(kwargs)
        return _FakeResponse()


async def test_upload_inbox_document_sends_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_server, "_client_for", lambda ctx: _FakeClient(record)
    )
    company_id = _uuid()
    payload = b"JPEGBYTES"
    result = await mcp_server.upload_inbox_document(
        ctx=None,
        filename="receipt.jpg",
        content_base64=base64.b64encode(payload).decode(),
        content_type="image/jpeg",
        company_id=company_id,
    )
    assert result["duplicate"] is False
    assert record["path"] == "/api/v1/inbox/documents"
    assert record["data"] == {"company_id": company_id}
    filename, raw, content_type = record["files"]["file"]
    assert filename == "receipt.jpg"
    assert raw == payload  # decoded bytes, not the base64 text
    assert content_type == "image/jpeg"
