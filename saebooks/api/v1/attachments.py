"""JSON router — ``/api/v1/attachments``.

Phase 1 of the saebooks-vault integration. The accounting DB stores no
blob bytes; this router is a tenant-scoped facade in front of
``saebooks-vault`` that:

1. Authenticates the caller through the standard ``require_bearer``
   dependency, so all v1 auth rules apply (JWT or static dev token).
2. Resolves the tenant from the bearer claims via ``resolve_tenant_id``
   and forwards it to the vault as ``X-Tenant-Id``. The vault enforces
   tenant isolation a second time using the same UUID, so even a
   compromised saebooks process can't read another tenant's files
   without forging a JWT.
3. Validates the (entity_kind, entity_id) target by SELECT-ing against
   the saebooks DB *under RLS*. This is the part the vault can't do —
   the vault has no idea which UUIDs are real invoices vs. forged
   numbers — and it stops a logged-in user from linking blobs to
   entities they don't actually own. Combined with the vault's own
   tenant gate, this is defence in depth.

What's NOT here (Phase 1 by design)
-----------------------------------
* No preview pipeline (vault has a 501 stub; we'll wire it once the
  vault grows real PDF + image rendering).
* No OCR / extraction (the vault is the natural place for the
  worker; saebooks just becomes a downstream consumer of OCR text).
* No bulk endpoints (re-link, move, batch-delete) — out of scope for
  the first cut. The single-file CRUD here is enough to ship the UI
  attachment panel.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings
from saebooks.models.bill import Bill
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNote
from saebooks.models.expense import Expense
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry
from saebooks.models.payment import Payment
from saebooks.services import vault as vault_client

logger = logging.getLogger("saebooks.api.attachments")

router = APIRouter(
    prefix="/attachments",
    tags=["attachments"],
    dependencies=[Depends(require_bearer)],
)


# Whitelist of entity kinds — also the saebooks-side existence-check
# table. Adding a new kind: append the model class here, run the
# tenant-scoped existence check, and the rest works unchanged.
_ENTITY_MODELS: dict[str, type] = {
    "invoice": Invoice,
    "bill": Bill,
    "payment": Payment,
    "contact": Contact,
    # Document Inbox phase 0 (issue #33) — publish attaches the source
    # document to the DRAFT expense it becomes. Expense carries ``id`` +
    # ``tenant_id``, satisfying the _assert_entity_owned SELECT below.
    "expense": Expense,
    # Document Inbox phase 2 (issue #33) — CREDIT_NOTE joins the publish
    # kinds; the source document links against the DRAFT credit note.
    # CreditNote carries ``id`` + ``tenant_id``, satisfying the
    # _assert_entity_owned SELECT below.
    "credit_note": CreditNote,
    # Cashbook entries are journal entries (CashbookEntryOut.id ==
    # JournalEntry.id), so receipts captured from a cashbook entry
    # link against JournalEntry.
    "journal_entry": JournalEntry,
}


def _require_enabled() -> None:
    """Hard gate — vault disabled or unconfigured returns 503.

    Kept as a function (not a dependency) so the disabled instance still
    publishes the routes in the OpenAPI spec — clients see "this endpoint
    exists but is currently off" rather than the route silently 404'ing.
    """
    if not settings.vault_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "attachments are not enabled on this saebooks instance",
        )
    if not settings.vault_shared_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "VAULT_SHARED_SECRET is not configured",
        )


def _validate_kind(entity_kind: str) -> type:
    """Return the model class for ``entity_kind`` or 422.

    Returning the class (rather than a bool) means the call site can
    pass it straight to the existence check without a second lookup.
    """
    model = _ENTITY_MODELS.get(entity_kind)
    if model is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown entity_kind '{entity_kind}'; "
            f"supported: {sorted(_ENTITY_MODELS)}",
        )
    return model


async def _assert_entity_owned(
    session: AsyncSession,
    model: type,
    entity_id: UUID,
    tenant_id: UUID,
) -> None:
    """Confirm (entity_id, tenant_id) maps to a real saebooks row.

    Runs under the RLS-bound session, so a foreign-tenant UUID returns
    None even if the caller knows it. We *also* check ``tenant_id ==``
    explicitly — belt-and-braces in case a future change reverts the
    RLS policy on one of these tables.
    """
    stmt = select(model.id).where(
        model.id == entity_id, model.tenant_id == tenant_id
    )
    found = (await session.execute(stmt)).scalar_one_or_none()
    if found is None:
        # 403 (not 404) — the caller is asking us to mutate something
        # they don't own. 404 would leak existence; 403 is the correct
        # answer regardless of whether the row is missing or foreign.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "entity does not exist for this tenant",
        )


def _actor_from_request(request: Request) -> str:
    """Best-effort actor string for vault audit (X-Actor header).

    We pass username when available (JWT path) or a short token-prefix
    otherwise. The vault stores it verbatim on each row.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"saebooks:{getattr(user, 'username', user.id)}"
    return "saebooks:api-token"


def _map_vault_error(exc: Exception) -> HTTPException:
    """Translate vault-client exceptions to clean HTTP responses."""
    if isinstance(exc, vault_client.VaultNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
    if isinstance(exc, vault_client.VaultUnauthorized):
        # Vault rejected our shared secret. The caller did nothing
        # wrong — surface as 502 (bad gateway) so they retry / alert.
        logger.error("vault rejected our bearer: %s", exc)
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY, "vault auth misconfigured"
        )
    if isinstance(exc, vault_client.VaultUnavailable):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "vault unavailable")
    if isinstance(exc, vault_client.VaultRequestError):
        # 4xx the vault returned for content reasons (size, bad mime,
        # etc.). Pass through the status code so the UI sees the same
        # error the vault did.
        return HTTPException(exc.status_code, f"vault error: {exc.body[:200]}")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "vault call failed")


def _shape_attachment(file_meta: dict[str, Any]) -> dict[str, Any]:
    """Normalise the vault payload for saebooks consumers.

    The saebooks UI doesn't care about ``preview_state`` or
    ``blob_db`` — we surface a smaller, stable shape. Anything we drop
    here can be added back without migrating clients.
    """
    return {
        "id": file_meta["id"],
        "filename": file_meta["filename"],
        "content_type": file_meta.get("mime"),
        "size": file_meta.get("size_bytes"),
        "sha256": file_meta.get("sha256"),
        "uploaded_by": file_meta.get("uploaded_by"),
        "uploaded_at": file_meta.get("uploaded_at"),
        "archived_at": file_meta.get("archived_at"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    request: Request,
    entity_kind: str = Form(...),
    entity_id: UUID = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Upload a blob, link it to the given saebooks entity.

    Two upstream calls: ``POST /files`` (upload) then
    ``POST /files/{id}/links`` (link). If the link fails after the
    upload succeeded, we surface the link error but the blob is left
    in the vault — the UI can re-link or the operator can clean up
    via the vault directly. We prefer "leaked blob, surfaced error"
    over "lost blob, hidden error".
    """
    _require_enabled()
    model = _validate_kind(entity_kind)
    tenant_id = resolve_tenant_id(request)
    await _assert_entity_owned(session, model, entity_id, tenant_id)

    payload = await file.read()
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")

    actor = _actor_from_request(request)
    try:
        meta = await vault_client.upload(
            tenant_id,
            file=payload,
            filename=file.filename or "unnamed",
            content_type=file.content_type,
            actor=actor,
        )
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc

    file_id = uuid.UUID(meta["id"])
    try:
        await vault_client.link(
            tenant_id,
            file_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            actor=actor,
        )
    except vault_client.VaultError as exc:
        # Don't try to rollback the upload — leaving an unlinked blob
        # is preferable to silently dropping the user's file.
        logger.warning(
            "vault upload OK but link failed (file=%s entity=%s/%s): %s",
            file_id, entity_kind, entity_id, exc,
        )
        raise _map_vault_error(exc) from exc

    return JSONResponse(_shape_attachment(meta), status_code=status.HTTP_201_CREATED)


@router.get("")
async def list_attachments(
    request: Request,
    entity_kind: str = Query(...),
    entity_id: UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """List attachments linked to the given saebooks entity.

    Both filters are required — listing every blob in the tenant from
    here would just be a wrapper around the vault's own list endpoint.
    """
    _require_enabled()
    model = _validate_kind(entity_kind)
    tenant_id = resolve_tenant_id(request)
    await _assert_entity_owned(session, model, entity_id, tenant_id)
    try:
        rows = await vault_client.list_files(
            tenant_id, entity_kind=entity_kind, entity_id=entity_id
        )
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc
    return [_shape_attachment(r) for r in rows]


@router.get("/{file_id}")
async def get_attachment(
    request: Request,
    file_id: UUID,
) -> dict[str, Any]:
    """Fetch metadata for a single attachment (no blob)."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    try:
        meta = await vault_client.get_file(tenant_id, file_id)
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc
    return _shape_attachment(meta)


@router.get("/{file_id}/download")
async def download_attachment(
    request: Request,
    file_id: UUID,
) -> StreamingResponse:
    """Stream the blob bytes from the vault to the client.

    We don't buffer the whole blob in saebooks memory — chunks are
    relayed as they arrive. Vault enforces the upload size cap, so
    the on-the-wire footprint is bounded.
    """
    _require_enabled()
    tenant_id = resolve_tenant_id(request)

    # Pre-flight metadata to set Content-Disposition / mime correctly
    # without waiting for the streaming response's first chunk.
    try:
        meta = await vault_client.get_file(tenant_id, file_id)
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc

    mime = meta.get("mime") or "application/octet-stream"
    filename = meta.get("filename") or "download"

    async def _gen():
        try:
            async for chunk, _m, _f in vault_client.stream_download(
                tenant_id, file_id
            ):
                yield chunk
        except vault_client.VaultError as exc:
            # Mid-stream failure: we've already sent headers, so the
            # best we can do is log and end the stream early. The
            # client gets a truncated body.
            logger.warning("vault stream interrupted: %s", exc)
            return

    return StreamingResponse(
        _gen(),
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    request: Request,
    file_id: UUID,
) -> Response:
    """Soft-delete an attachment (vault sets ``archived_at``).

    Idempotent — deleting an already-archived file still returns 204.
    """
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    try:
        await vault_client.delete(tenant_id, file_id)
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
