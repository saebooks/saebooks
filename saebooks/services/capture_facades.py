"""Delegation facades for the capture module env-flag (#32 step 5).

When ``settings.capture_base_url`` is set, the engine's capture surface routes
its work here. Two styles (see ``capture_client`` for the rationale):

* ``extract_document`` — a service-level facade with the SAME signature as the
  in-process ``services.ai_extraction.extract_document``; posts the file bytes
  as multipart and returns the module's dict unchanged.
* ``mirror_post`` / ``mirror_get`` — route-level proxies for the imports
  wizard + bank-feeds REST handlers. They forward the request to the module
  and return a ``JSONResponse`` that mirrors the module's status code and JSON
  body verbatim, so the API client sees the identical response it would from
  the in-process path. The module owns idempotency / change-log / commit in
  delegated mode; the engine handler is a pass-through.

Imports lazily to avoid an import cycle (the service modules import THIS
module at load time).
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi.responses import JSONResponse

from saebooks.services import capture_client as _client

# Response headers worth preserving when mirroring a module response back to
# the original API client. ``Retry-After`` matters for the idempotency
# in-flight 503 the wizard handlers can emit.
_FORWARD_RESPONSE_HEADERS = ("retry-after",)


def _mirror(resp: Any, path: str) -> JSONResponse:
    """Turn a module ``httpx.Response`` into a mirrored ``JSONResponse``."""
    body = _client.json_body(resp, path)
    headers = {
        k: resp.headers[k]
        for k in _FORWARD_RESPONSE_HEADERS
        if k in resp.headers
    }
    return JSONResponse(
        content=body, status_code=resp.status_code, headers=headers or None
    )


# --------------------------------------------------------------------------- #
# ai_extraction — service-level facade                                          #
# --------------------------------------------------------------------------- #
async def extract_document(
    file_bytes: bytes,
    mime_type: str,
) -> dict[str, Any]:
    """Delegate document extraction to the capture module.

    Mirrors ``services.ai_extraction.extract_document``'s return contract: the
    module runs the LLM call in-process and returns the canonical dict, which
    we pass straight through. A 503 from the module (LiteLLM not configured)
    is re-raised as ``AiExtractionNotConfiguredError`` so the engine router
    maps it to the same 503 it would in-process.
    """
    from saebooks.services.ai_extraction import AiExtractionNotConfiguredError

    resp = await _client.post_multipart(
        "documents/extract",
        file_bytes=file_bytes,
        filename="upload",
        mime_type=mime_type,
    )
    if resp.status_code == 503:
        body = _client.json_body(resp, "documents/extract") or {}
        raise AiExtractionNotConfiguredError(
            body.get("detail", "capture module: AI extraction not configured")
        )
    return _client.ensure_ok(resp, "documents/extract")


# --------------------------------------------------------------------------- #
# imports + bank-feeds — route-level proxies                                    #
# --------------------------------------------------------------------------- #
async def mirror_post(
    path: str,
    raw_body: bytes,
    *,
    content_type: str,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
) -> JSONResponse:
    """Forward a POST request body verbatim and mirror the module response."""
    resp = await _client.post_raw(
        path,
        raw_body,
        content_type=content_type or "application/json",
        tenant_id=tenant_id,
        company_id=company_id,
        idempotency_key=idempotency_key,
    )
    return _mirror(resp, path)


async def mirror_get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
) -> JSONResponse:
    """Forward a GET request and mirror the module response."""
    resp = await _client.get(
        path, params=params, tenant_id=tenant_id, company_id=company_id
    )
    return _mirror(resp, path)


async def mirror_delete(
    path: str,
    *,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
) -> JSONResponse:
    """Forward a DELETE request and mirror the module response."""
    resp = await _client.delete(
        path, tenant_id=tenant_id, company_id=company_id
    )
    return _mirror(resp, path)
