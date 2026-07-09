"""Engine-side HTTP client for the capture module (#32 step 5).

The engine's imports / bank-feeds / ai-extraction route handlers (and the
``services.ai_extraction`` service function) call into here when
``settings.capture_base_url`` is set. It is the render/comms/pre-accounting
facade pattern applied to the capture surface: the call goes over HTTP to a
sibling ``capture-web`` container that runs the SAME code against the SAME
database, and the response is mirrored back to the caller.

Two delegation styles are used, matching the shape of what is being
delegated:

* **service-level** (``ai_extraction``) — the engine's ``extract_document``
  service function has a clean signature, so its facade posts the file bytes
  as multipart and returns the module's parsed dict, exactly like the
  pre-accounting service facades.
* **route-level proxy** (``imports`` wizard + ``bank-feeds`` REST) — those
  surfaces live entirely in route handlers (there is no narrow service layer;
  the wizard handlers own idempotency, change-log and commit-dispatch). The
  faithful delegation is therefore to forward the raw request to the module
  and mirror its response (status + body) verbatim. The module owns ALL the
  logic in delegated mode; the engine handler is a pure pass-through.

Contract per call
-----------------
``{capture_base_url}/module/capture/{path}``

Headers::

    X-Capture-Token: {capture_service_token}   (when non-empty)
    X-Tenant-Id:     {tenant_id}               (RLS context)
    X-Company-Id:    {company_id}              (when known)
    X-Idempotency-Key: {key}                   (forwarded when present)

The tenant/company headers carry the RLS context the module needs to bind
``app.current_tenant`` — the module has no JWT of its own.

Errors
------
* Transport failure / timeout raise ``CaptureServiceError`` so a misconfigured
  flag fails loud rather than silently returning wrong data.
* HTTP status codes and JSON bodies from the module are mirrored back to the
  original caller (the module already speaks the engine's error contract), so
  a 404 / 409 / 422 / 503 the module returns is the status the API client
  sees — identical to the in-process path.
"""
from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

import httpx

_TIMEOUT_SECONDS = 60.0  # generous — AI extraction round-trips to LiteLLM
_BASE_PATH = "/module/capture"


class CaptureServiceError(RuntimeError):
    """The capture module was unreachable or answered unexpectedly."""


def delegating() -> bool:
    """True when the engine should delegate capture work to the module."""
    from saebooks.config import settings

    return bool(settings.capture_base_url.strip())


def jsonable(value: Any) -> Any:
    """Recursively coerce UUID / date / datetime / Decimal into JSON scalars."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def _headers(
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None,
    *,
    idempotency_key: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    from saebooks.config import settings

    headers: dict[str, str] = {}
    token = settings.capture_service_token.strip()
    if token:
        headers["X-Capture-Token"] = token
    if tenant_id is not None:
        headers["X-Tenant-Id"] = str(tenant_id)
    if company_id is not None:
        headers["X-Company-Id"] = str(company_id)
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    if extra:
        headers.update(extra)
    return headers


def _url(path: str) -> str:
    from saebooks.config import settings

    base = settings.capture_base_url.rstrip("/")
    return f"{base}{_BASE_PATH}/{path.lstrip('/')}"


async def post_json(
    path: str,
    payload: dict[str, Any],
    *,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """POST a JSON body to the module and return the raw response."""
    url = _url(path)
    headers = _headers(
        tenant_id, company_id, idempotency_key=idempotency_key
    )
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(url, json=jsonable(payload), headers=headers)
    except httpx.TimeoutException as exc:
        raise CaptureServiceError(
            f"Timeout waiting for capture module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise CaptureServiceError(
            f"Cannot reach capture module at {url}: {exc}"
        ) from exc


async def post_raw(
    path: str,
    content: bytes,
    *,
    content_type: str,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """POST a raw (already-serialised) body to the module — used to forward a
    request body verbatim in the route-level proxy path."""
    url = _url(path)
    headers = _headers(
        tenant_id, company_id, idempotency_key=idempotency_key
    )
    headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as exc:
        raise CaptureServiceError(
            f"Timeout waiting for capture module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise CaptureServiceError(
            f"Cannot reach capture module at {url}: {exc}"
        ) from exc


async def post_multipart(
    path: str,
    *,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    tenant_id: uuid.UUID | str | None = None,
    company_id: uuid.UUID | str | None = None,
) -> httpx.Response:
    """POST a multipart file upload to the module (ai-extraction path)."""
    url = _url(path)
    headers = _headers(tenant_id, company_id)
    files = {"file": (filename, file_bytes, mime_type)}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(url, files=files, headers=headers)
    except httpx.TimeoutException as exc:
        raise CaptureServiceError(
            f"Timeout waiting for capture module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise CaptureServiceError(
            f"Cannot reach capture module at {url}: {exc}"
        ) from exc


async def get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
) -> httpx.Response:
    """GET from the module and return the raw response."""
    url = _url(path)
    headers = _headers(tenant_id, company_id)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise CaptureServiceError(
            f"Timeout waiting for capture module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise CaptureServiceError(
            f"Cannot reach capture module at {url}: {exc}"
        ) from exc


async def delete(
    path: str,
    *,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
) -> httpx.Response:
    """DELETE on the module and return the raw response."""
    url = _url(path)
    headers = _headers(tenant_id, company_id)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.delete(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise CaptureServiceError(
            f"Timeout waiting for capture module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise CaptureServiceError(
            f"Cannot reach capture module at {url}: {exc}"
        ) from exc


def json_body(resp: httpx.Response, url_hint: str = "") -> Any:
    """Parse a response body as JSON, tolerating an empty body (→ None)."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise CaptureServiceError(
            f"capture module returned non-JSON body"
            f"{(' for ' + url_hint) if url_hint else ''}: {resp.text[:200]}"
        ) from exc


def ensure_ok(resp: httpx.Response, path: str) -> Any:
    """Return the JSON body for a 2xx response, else raise transport error."""
    if resp.status_code // 100 != 2:
        raise CaptureServiceError(
            f"capture module {path} returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    return json_body(resp, path)
