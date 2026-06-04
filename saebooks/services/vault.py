"""Vault client — thin async wrapper around ``saebooks-vault`` REST.

Phase 1 of the saebooks-vault integration. The accounting core
intentionally stores **no blob bytes** itself: attachments live in the
closed-source vault service, identified here only by the UUID the vault
returns at upload. This module is the single chokepoint for outbound
HTTP to the vault — every request injects the bearer secret + the
``X-Tenant-Id`` header, so callers don't have to thread auth through
each call site.

Why a per-call client (not a module-level singleton)
----------------------------------------------------
``httpx.AsyncClient`` keeps a connection pool, but pinning a single
client at import time complicates testing (every test would need to
patch the module-level instance) and ties the pool's lifetime to the
process. The cost of constructing a client per request is negligible
against vault round-trip latency, and it keeps the seams clean —
each public function opens its own client in an ``async with`` block
so connections are returned promptly.

Errors
------
Vault HTTP errors map to typed exceptions so the saebooks-side
attachments router can translate them to clean HTTP responses without
sniffing strings:

* ``VaultUnauthorized`` — 401 / 403 from the vault. Misconfigured
  shared secret on either side.
* ``VaultNotFound``     — 404. File does not exist (or belongs to
  another tenant — vault refuses to disclose which).
* ``VaultUnavailable``  — connect error, timeout, or 5xx. Treat as
  transient; surface 502 to the user.

Anything else (4xx that isn't 401/404) bubbles up as
``VaultRequestError`` carrying the upstream status + body so the caller
can propagate or log.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

import httpx

from saebooks.config import settings

logger = logging.getLogger("saebooks.services.vault")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """Base class for vault client errors."""


class VaultUnavailable(VaultError):
    """Vault is unreachable / 5xx / timing out."""


class VaultUnauthorized(VaultError):
    """Vault rejected our bearer token (401 / 403)."""


class VaultNotFound(VaultError):
    """Vault returned 404 for the requested file id."""


class VaultRequestError(VaultError):
    """Catch-all for other 4xx responses from the vault."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"vault returned {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Return the vault base URL with no trailing slash."""
    return settings.vault_url.rstrip("/")


def _headers(tenant_id: uuid.UUID, actor: str | None = None) -> dict[str, str]:
    if not settings.vault_shared_secret:
        # Defensive — handlers should also gate on vault_enabled, but we
        # never want to silently send a request with no auth header.
        raise VaultUnauthorized(
            "VAULT_SHARED_SECRET is not configured on this saebooks instance"
        )
    h = {
        "Authorization": f"Bearer {settings.vault_shared_secret}",
        "X-Tenant-Id": str(tenant_id),
    }
    if actor:
        h["X-Actor"] = actor
    return h


def _raise_for_status(resp: httpx.Response) -> None:
    """Map upstream status codes to our typed exception ladder."""
    if resp.status_code < 400:
        return
    if resp.status_code in (401, 403):
        raise VaultUnauthorized(f"vault auth rejected: {resp.text[:200]}")
    if resp.status_code == 404:
        raise VaultNotFound(resp.text[:200] or "file not found")
    if resp.status_code >= 500:
        raise VaultUnavailable(
            f"vault {resp.status_code}: {resp.text[:200]}"
        )
    raise VaultRequestError(resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# Public surface — one function per vault operation we use
# ---------------------------------------------------------------------------


async def upload(
    tenant_id: uuid.UUID,
    *,
    file: bytes | BinaryIO,
    filename: str,
    content_type: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/files — upload a blob, return vault metadata.

    Returns the vault's ``FileOut`` shape verbatim:
    ``{id, tenant_id, filename, mime, size_bytes, sha256, ...}``.
    """
    url = f"{_base_url()}/api/v1/files"
    files = {"upload": (filename, file, content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=settings.vault_upload_timeout) as client:
            resp = await client.post(url, files=files, headers=_headers(tenant_id, actor))
    except httpx.HTTPError as exc:
        logger.warning("vault upload connect/timeout: %s", exc)
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)
    return resp.json()


async def list_files(
    tenant_id: uuid.UUID,
    *,
    entity_kind: str | None = None,
    entity_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """GET /api/v1/files — list files, optionally filtered by linkage."""
    url = f"{_base_url()}/api/v1/files"
    params: dict[str, str] = {}
    if entity_kind and entity_id:
        params["entity_kind"] = entity_kind
        params["entity_id"] = str(entity_id)
    if include_archived:
        params["include_archived"] = "true"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_timeout) as client:
            resp = await client.get(url, params=params, headers=_headers(tenant_id))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)
    return resp.json()


async def get_file(tenant_id: uuid.UUID, file_id: uuid.UUID) -> dict[str, Any]:
    """GET /api/v1/files/{id} — fetch metadata for a single file."""
    url = f"{_base_url()}/api/v1/files/{file_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_timeout) as client:
            resp = await client.get(url, headers=_headers(tenant_id))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)
    return resp.json()


async def download(
    tenant_id: uuid.UUID, file_id: uuid.UUID
) -> tuple[bytes, str, str]:
    """GET /api/v1/files/{id}/download — return ``(bytes, mime, filename)``.

    Streams the response body into memory. Vault enforces a max upload
    size at write time; same ceiling applies on the way out, so this
    is bounded.
    """
    url = f"{_base_url()}/api/v1/files/{file_id}/download"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_upload_timeout) as client:
            resp = await client.get(url, headers=_headers(tenant_id))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)
    mime = resp.headers.get("content-type", "application/octet-stream")
    # Pull filename out of the Content-Disposition header if present.
    cd = resp.headers.get("content-disposition", "")
    filename = ""
    if "filename=" in cd:
        filename = cd.split("filename=", 1)[1].strip().strip('"')
    return resp.content, mime, filename


async def stream_download(
    tenant_id: uuid.UUID, file_id: uuid.UUID
) -> AsyncIterator[tuple[bytes, str, str]]:
    """Streaming variant — yields ``(chunk, mime, filename)`` tuples.

    The first iteration carries mime + filename + first chunk; subsequent
    yields are ``(chunk, "", "")``. Used by the saebooks attachments
    router so we don't buffer the whole blob in process memory.
    """
    url = f"{_base_url()}/api/v1/files/{file_id}/download"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_upload_timeout) as client:  # noqa: SIM117  inner stream() uses `client` bound here
            async with client.stream(
                "GET", url, headers=_headers(tenant_id)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    # Re-build a non-streaming Response-like for mapping.
                    fake = httpx.Response(
                        status_code=resp.status_code, content=body.encode()
                    )
                    _raise_for_status(fake)
                mime = resp.headers.get("content-type", "application/octet-stream")
                cd = resp.headers.get("content-disposition", "")
                filename = ""
                if "filename=" in cd:
                    filename = cd.split("filename=", 1)[1].strip().strip('"')
                first = True
                async for chunk in resp.aiter_bytes(64 * 1024):
                    if first:
                        yield chunk, mime, filename
                        first = False
                    else:
                        yield chunk, "", ""
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc


async def delete(tenant_id: uuid.UUID, file_id: uuid.UUID) -> None:
    """DELETE /api/v1/files/{id} — soft-archive in the vault."""
    url = f"{_base_url()}/api/v1/files/{file_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_timeout) as client:
            resp = await client.delete(url, headers=_headers(tenant_id))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)


async def link(
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    actor: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/files/{id}/links — link an existing file to an entity."""
    url = f"{_base_url()}/api/v1/files/{file_id}/links"
    body = {"entity_kind": entity_kind, "entity_id": str(entity_id)}
    try:
        async with httpx.AsyncClient(timeout=settings.vault_timeout) as client:
            resp = await client.post(url, json=body, headers=_headers(tenant_id, actor))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    if resp.status_code == 409:
        # Already linked — treat as idempotent success at the saebooks
        # layer. Caller can still check the response shape if it cares.
        return {"already_linked": True}
    _raise_for_status(resp)
    return resp.json()


async def unlink(
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    link_id: uuid.UUID,
) -> None:
    url = f"{_base_url()}/api/v1/files/{file_id}/links/{link_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.vault_timeout) as client:
            resp = await client.delete(url, headers=_headers(tenant_id))
    except httpx.HTTPError as exc:
        raise VaultUnavailable(f"vault connection failed: {exc}") from exc
    _raise_for_status(resp)
