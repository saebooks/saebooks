"""JSON router — ``/api/v1/inbox`` (Document Inbox, issue #33 phase 1).

Thin HTTP shell over ``services/document_inbox.py`` — all inbox logic
(ingest funnel, state machine, completeness) lives in the service; this
module only parses/validates requests and shapes responses.

Gates (spec §3):

* ``require_bearer`` + ``require_feature(FLAG_DOCUMENT_INBOX)`` router-
  wide — the inbox does not exist below the Offline edition (404).
* The vault deployment gate (``_require_enabled`` — the attachments.py
  idiom) on every endpoint: an inbox instance without a configured
  vault returns 503, because documents cannot be stored or streamed.
* The extract-retry endpoint is additionally gated by the existing
  ``FLAG_AI_EXTRACTION`` (Business+): below that tier the route 404s
  and documents are keyed manually.

Publish (spec §6) is EXPENSE-only in phase 1 and goes through the same
service function behind ``POST /api/v1/expenses``
(``services/expenses.api_create``) — a DRAFT record, never a journal
entry, never auto-posted. ``X-Idempotency-Key`` is required and handled
with the standard ``services/idempotency`` claim/replay machinery.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.models.inbox_document import (
    InboxDocument,
    InboxDocumentSource,
    InboxDocumentStatus,
    PublishedRecordKind,
    RejectReason,
)
from saebooks.models.inbox_email import InboxEmailAddress
from saebooks.models.supplier_rule import SupplierRule
from saebooks.services import bills as bills_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import credit_notes as credit_notes_svc
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import expenses as expenses_svc
from saebooks.services import features
from saebooks.services import vault as vault_client
from saebooks.services.features import (
    FLAG_AI_EXTRACTION,
    FLAG_DOCUMENT_INBOX,
    FLAG_INBOX_EMAIL,
    require_feature,
)
from saebooks.services.idempotency import (
    ClaimStatus,
    claim_or_fetch,
    store_response,
)

logger = logging.getLogger("saebooks.api.document_inbox")

router = APIRouter(
    prefix="/inbox",
    tags=["document_inbox"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_DOCUMENT_INBOX)),
    ],
)

# Identical to the extract path (api/v1/ai_extraction.py) by design —
# nothing ingested is unextractable.
_SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
})
_HEIC_MIME_TYPES: frozenset[str] = frozenset({"image/heic", "image/heif"})
_MAX_BYTES = 10 * 1024 * 1024

# Publish record kind → attachments-facade entity kind (vault link).
_PUBLISH_ENTITY_KIND: dict[PublishedRecordKind, str] = {
    PublishedRecordKind.EXPENSE: "expense",
    PublishedRecordKind.BILL: "bill",
    PublishedRecordKind.CREDIT_NOTE: "credit_note",
}

# Default list view excludes terminal states (spec §3).
_TERMINAL = (
    InboxDocumentStatus.PUBLISHED.value,
    InboxDocumentStatus.REJECTED.value,
    InboxDocumentStatus.DUPLICATE.value,
)


# ---------------------------------------------------------------------------
# Gates + small helpers (attachments.py idioms)
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    """Vault deployment gate — 503 when the vault is off/unconfigured.

    Kept as a function (not a dependency) so the disabled instance still
    publishes the routes in the OpenAPI spec (attachments.py precedent).
    """
    if not settings.vault_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the document inbox requires saebooks-vault, which is not "
            "enabled on this instance",
        )
    if not settings.vault_shared_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "VAULT_SHARED_SECRET is not configured",
        )


def _actor_from_request(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"saebooks:{getattr(user, 'username', user.id)}"
    return "saebooks:api-token"


def _user_id_from_request(request: Request) -> uuid.UUID | None:
    user = getattr(request.state, "user", None)
    return getattr(user, "id", None) if user is not None else None


def _map_vault_error(exc: Exception) -> HTTPException:
    if isinstance(exc, vault_client.VaultNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "document blob not found")
    if isinstance(exc, vault_client.VaultUnauthorized):
        logger.error("vault rejected our bearer: %s", exc)
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "vault auth misconfigured")
    if isinstance(exc, vault_client.VaultUnavailable):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "vault unavailable")
    if isinstance(exc, vault_client.VaultRequestError):
        return HTTPException(exc.status_code, f"vault error: {exc.body[:200]}")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "vault call failed")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _shape_document(doc: InboxDocument) -> dict[str, Any]:
    """Stable JSON shape for one inbox document (list + detail)."""
    return {
        "id": str(doc.id),
        "company_id": str(doc.company_id) if doc.company_id else None,
        "vault_file_id": str(doc.vault_file_id),
        "sha256": doc.sha256,
        "filename": doc.filename,
        "mime": doc.mime,
        "size_bytes": doc.size_bytes,
        "source": str(doc.source),
        "source_ref": doc.source_ref,
        "status": str(doc.status),
        "extract": doc.extract,
        "extraction_override": doc.extraction_override,
        "extract_model": doc.extract_model,
        "extraction_confidence": (
            str(doc.extraction_confidence) if doc.extraction_confidence else None
        ),
        "extraction_error": doc.extraction_error,
        "extracted_at": _iso(doc.extracted_at),
        "attempt_count": doc.attempt_count,
        "last_error": doc.last_error,
        "duplicate_of_id": (
            str(doc.duplicate_of_id) if doc.duplicate_of_id else None
        ),
        "suggested_contact_id": (
            str(doc.suggested_contact_id) if doc.suggested_contact_id else None
        ),
        "suggested_account_id": (
            str(doc.suggested_account_id) if doc.suggested_account_id else None
        ),
        "suggested_tax_code_id": (
            str(doc.suggested_tax_code_id) if doc.suggested_tax_code_id else None
        ),
        "supplier_rule_id": (
            str(doc.supplier_rule_id) if doc.supplier_rule_id else None
        ),
        "published_record_kind": (
            str(doc.published_record_kind) if doc.published_record_kind else None
        ),
        "published_record_id": (
            str(doc.published_record_id) if doc.published_record_id else None
        ),
        "published_at": _iso(doc.published_at),
        "reject_reason": str(doc.reject_reason) if doc.reject_reason else None,
        "reject_note": doc.reject_note,
        "version": doc.version,
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
    }


def _shape_advisory_duplicate(doc: InboxDocument) -> dict[str, Any]:
    """Compact sibling shape for the advisory near-duplicate banner —
    enough to render a warning with links, not the full document."""
    merged = inbox_svc.merged_extract(doc)
    return {
        "id": str(doc.id),
        "status": str(doc.status),
        "source": str(doc.source),
        "filename": doc.filename,
        "vendor_name": merged.get("vendor_name"),
        "invoice_number": merged.get("invoice_number"),
        "total": merged.get("total"),
        "created_at": _iso(doc.created_at),
    }


async def _get_document(
    session: AsyncSession,
    document_id: UUID,
    tenant_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> InboxDocument:
    """Tenant-filtered fetch (belt) under the RLS session (braces) or 404.

    ``for_update=True`` takes ``SELECT … FOR UPDATE`` — the terminal
    transitions (publish / reject) lock the row so two interleaved
    requests serialise: the loser re-reads the winner's committed status
    and 409s in ``ensure_can_transition`` instead of double-creating.
    """
    stmt = select(InboxDocument).where(
        InboxDocument.id == document_id,
        InboxDocument.tenant_id == tenant_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "inbox document not found")
    return doc


async def _validate_company(
    session: AsyncSession, company_id: UUID, tenant_id: uuid.UUID
) -> None:
    found = (
        await session.execute(
            select(Company.id).where(
                Company.id == company_id,
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")


def _conflict_from(exc: inbox_svc.IllegalTransitionError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class InboxDocumentPatch(BaseModel):
    """PATCH body — reviewer edits. ``extract`` is immutable by design;
    only the override, the company routing, the suggestion overrides
    (spec §3) and the optimistic-lock version are accepted
    (``extra="forbid"`` turns anything else into a 422 rather than
    silently dropping it).
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    extraction_override: dict[str, Any] | None = None
    company_id: UUID | None = None
    # Suggestion overrides — the reviewer may correct or clear what a
    # supplier rule filled. Sending null clears the suggestion.
    suggested_contact_id: UUID | None = None
    suggested_account_id: UUID | None = None
    suggested_tax_code_id: UUID | None = None


class PublishLine(BaseModel):
    """One coded expense line — the ``line amount = unit_price``
    convention, matching ``ExpenseLineCreate``."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    account_id: UUID
    tax_code_id: UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    project_id: UUID | None = None


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_kind: str
    company_id: UUID
    contact_id: UUID
    date: date
    # Required for EXPENSE only — an expense is paid at checkout, so the
    # credited payment account is part of its identity. (Addition to the
    # spec §3 body, which omitted it; ``create_expense`` cannot run
    # without one.) BILL / CREDIT_NOTE ignore it.
    payment_account_id: UUID | None = None
    # BILL only; omitted → derived from the supplier's payment terms.
    due_date: date | None = None
    reference: str | None = None
    lines: list[PublishLine] = Field(min_length=1)
    notes: str | None = None
    # Supplier-rule learning (phase 2, spec §6): learn_rule upserts a
    # LEARNED rule from the confirmed values; update_rule rewrites an
    # existing rule's defaults to them.
    learn_rule: bool = False
    update_rule: bool = False


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: RejectReason
    note: str | None = None


class SupplierRuleCreate(BaseModel):
    """POST body — a MANUAL rule. ``vendor_name`` is normalised into
    the stored ``vendor_key`` by the service."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: str = Field(min_length=1)
    contact_id: UUID
    company_id: UUID | None = None
    vendor_abn: str | None = None
    account_id: UUID | None = None
    tax_code_id: UUID | None = None
    record_kind: str | None = None


class SupplierRulePatch(BaseModel):
    """PATCH body — partial update; ``active: false`` is the soft
    delete (spec §3). Omitted fields are untouched; explicit null clears
    a nullable column."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = Field(default=None, min_length=1)
    vendor_abn: str | None = None
    contact_id: UUID | None = None
    company_id: UUID | None = None
    account_id: UUID | None = None
    tax_code_id: UUID | None = None
    record_kind: str | None = None
    active: bool | None = None


# ---------------------------------------------------------------------------
# Upload (spec §3/§4 — one multipart endpoint for every surface)
# ---------------------------------------------------------------------------


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    company_id: Annotated[UUID | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Capture a document into the inbox.

    Duplicate content (same bytes already live in this tenant's inbox)
    returns **200 with the existing row + ``"duplicate": true``** — a
    mobile double-tap must not read as failure. Otherwise 201.
    """
    _require_enabled()
    tenant_id = resolve_tenant_id(request)

    mime = file.content_type or ""
    if mime in _HEIC_MIME_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "HEIC/HEIF images are not supported — convert to JPEG and "
            "re-upload (iPhone: Settings → Camera → Formats → Most Compatible).",
        )
    if mime not in _SUPPORTED_MIME_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported file type '{mime}'. "
            f"Accepted: {', '.join(sorted(_SUPPORTED_MIME_TYPES))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    if len(data) > _MAX_BYTES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"File too large ({len(data)} bytes); maximum is {_MAX_BYTES} bytes.",
        )

    if company_id is not None:
        await _validate_company(session, company_id, tenant_id)

    try:
        doc, duplicate = await inbox_svc.ingest(
            session,
            tenant_id,
            data=data,
            filename=file.filename or "unnamed",
            mime=mime,
            source=InboxDocumentSource.UPLOAD,
            company_id=company_id,
            actor=_actor_from_request(request),
            created_by=_user_id_from_request(request),
            # Extraction is tier-gated separately (Business+). Below that
            # tier documents land NEEDS_REVIEW empty for manual keying.
            extract_enabled=features.is_enabled(FLAG_AI_EXTRACTION),
        )
    except vault_client.VaultError as exc:
        raise _map_vault_error(exc) from exc

    body = _shape_document(doc)
    body["duplicate"] = duplicate
    return JSONResponse(
        body,
        status_code=status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# List / detail / download
# ---------------------------------------------------------------------------


@router.get("/documents")
async def list_documents(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    source: str | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Paginated inbox list — newest first; terminal states excluded
    unless explicitly requested via ``status=``."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)

    where = [InboxDocument.tenant_id == tenant_id]
    if status_filter is not None:
        try:
            where.append(
                InboxDocument.status == InboxDocumentStatus(status_filter.upper()).value
            )
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status_filter}'") from exc
    else:
        where.append(InboxDocument.status.notin_(_TERMINAL))
    if source is not None:
        try:
            where.append(
                InboxDocument.source == InboxDocumentSource(source.upper()).value
            )
        except ValueError as exc:
            raise HTTPException(400, f"Invalid source '{source}'") from exc
    if company_id is not None:
        where.append(InboxDocument.company_id == company_id)

    total = (
        await session.execute(
            select(func.count()).select_from(InboxDocument).where(*where)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(InboxDocument)
            .where(*where)
            .order_by(InboxDocument.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).scalars().all()

    return {
        "items": [_shape_document(d) for d in rows],
        "total": total,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }


@router.get("/stats")
async def inbox_stats(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Badge + alerting numbers (spec §3)."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)

    tracked = (
        InboxDocumentStatus.RECEIVED.value,
        InboxDocumentStatus.NEEDS_REVIEW.value,
        InboxDocumentStatus.READY.value,
        InboxDocumentStatus.FAILED.value,
    )
    rows = (
        await session.execute(
            select(InboxDocument.status, func.count())
            .where(
                InboxDocument.tenant_id == tenant_id,
                InboxDocument.status.in_(tracked),
            )
            .group_by(InboxDocument.status)
        )
    ).all()
    counts = {s: 0 for s in tracked}
    counts.update({row[0]: row[1] for row in rows})

    oldest = (
        await session.execute(
            select(func.min(InboxDocument.created_at)).where(
                InboxDocument.tenant_id == tenant_id,
                InboxDocument.status == InboxDocumentStatus.RECEIVED.value,
            )
        )
    ).scalar_one_or_none()
    age_s = (
        max(0, int((datetime.now(UTC) - oldest).total_seconds()))
        if oldest is not None
        else None
    )

    return {
        **counts,
        "oldest_unextracted_age_s": age_s,
        # Phase 4: open documents whose (contact/vendor, invoice_number)
        # identity collides with another open document — the advisory
        # near-duplicate counter for the badge/alerting surfaces.
        "advisory_duplicates": await inbox_svc.count_advisory_duplicates(
            session, tenant_id
        ),
    }


@router.get("/documents/{document_id}")
async def get_document(
    request: Request,
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    doc = await _get_document(session, document_id, tenant_id)
    body = _shape_document(doc)
    # Advisory near-duplicate (phase 4): non-terminal siblings that look
    # like the same invoice — same (contact/vendor, invoice_number) —
    # which byte-level sha256 dedupe cannot catch on a re-scan. Advisory
    # only: the review banner warns, nothing is blocked.
    body["advisory_duplicates"] = [
        _shape_advisory_duplicate(d)
        for d in await inbox_svc.find_advisory_duplicates(session, doc)
    ]
    return body


@router.get("/documents/{document_id}/download")
async def download_document(
    request: Request,
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream the source blob through the engine (no presigned URLs)."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    doc = await _get_document(session, document_id, tenant_id)

    async def _gen():
        try:
            async for chunk, _m, _f in vault_client.stream_download(
                tenant_id, doc.vault_file_id
            ):
                yield chunk
        except vault_client.VaultError as exc:
            # Headers already sent — log and end the stream early.
            logger.warning("vault stream interrupted: %s", exc)
            return

    return StreamingResponse(
        _gen(),
        media_type=doc.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Review (PATCH) — extraction_override / company_id only
# ---------------------------------------------------------------------------


@router.patch("/documents/{document_id}")
async def patch_document(
    request: Request,
    document_id: UUID,
    payload: InboxDocumentPatch,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Reviewer edits. ``extract`` is never mutated; writes go to
    ``extraction_override`` (and ``company_id`` routing). Requires the
    current ``version`` — 409 on mismatch. Recomputes NEEDS_REVIEW ↔
    READY completeness after the write."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    doc = await _get_document(session, document_id, tenant_id)

    # Terminal documents are immutable provenance (spec §6): a PUBLISHED
    # row records exactly what produced the ledger record; REJECTED /
    # DUPLICATE are closed audit rows. No edits, ever.
    if InboxDocumentStatus(doc.status) in inbox_svc.TERMINAL_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"document is {InboxDocumentStatus(doc.status).value} — "
            "terminal documents are immutable",
        )

    if payload.version != doc.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"version mismatch: expected {doc.version}, got {payload.version}",
        )

    fields_set = payload.model_fields_set
    if "extraction_override" in fields_set:
        doc.extraction_override = payload.extraction_override
    if "company_id" in fields_set:
        if payload.company_id is not None:
            await _validate_company(session, payload.company_id, tenant_id)
        doc.company_id = payload.company_id
    # Suggestion overrides (phase 2) — tenant-ownership checked; null clears.
    suggestion_fields = {
        "suggested_contact_id",
        "suggested_account_id",
        "suggested_tax_code_id",
    } & fields_set
    if suggestion_fields:
        try:
            await inbox_svc.validate_coding_fks(
                session,
                tenant_id,
                contact_id=payload.suggested_contact_id,
                account_id=payload.suggested_account_id,
                tax_code_id=payload.suggested_tax_code_id,
            )
        except inbox_svc.SupplierRuleError as exc:
            raise HTTPException(422, str(exc)) from exc
        for field in suggestion_fields:
            setattr(doc, field, getattr(payload, field))

    doc.version += 1
    try:
        inbox_svc.recompute_completeness(doc)
    except inbox_svc.IllegalTransitionError as exc:  # pragma: no cover — guarded above
        raise _conflict_from(exc) from exc

    await change_log_svc.append(
        session,
        entity="inbox_document",
        entity_id=doc.id,
        op="update",
        actor=_actor_from_request(request),
        payload=_shape_document(doc),
        version=doc.version,
        tenant_id=tenant_id,
    )
    await session.commit()
    await session.refresh(doc)  # reload server-touched updated_at for the shape
    return _shape_document(doc)


# ---------------------------------------------------------------------------
# Supplier rules (spec §3/§6 phase 2) — suggestion-only vendor coding
# ---------------------------------------------------------------------------


def _shape_rule(rule: SupplierRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "company_id": str(rule.company_id) if rule.company_id else None,
        "vendor_key": rule.vendor_key,
        "vendor_abn": rule.vendor_abn,
        "contact_id": str(rule.contact_id),
        "account_id": str(rule.account_id) if rule.account_id else None,
        "tax_code_id": str(rule.tax_code_id) if rule.tax_code_id else None,
        "record_kind": rule.record_kind,
        "origin": str(rule.origin),
        "times_applied": rule.times_applied,
        "times_overridden": rule.times_overridden,
        "last_applied_at": _iso(rule.last_applied_at),
        "created_from_document_id": (
            str(rule.created_from_document_id)
            if rule.created_from_document_id
            else None
        ),
        "active": rule.active,
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
    }


async def _rule_integrity_http(
    session: AsyncSession, exc: IntegrityError
) -> HTTPException:
    """The partial-unique (one active rule per vendor per scope)
    surfaces as 409 — the caller edits/reactivates the existing rule
    instead; anything else DB-constraint-shaped is a 422."""
    await session.rollback()
    if "uq_supplier_rules_scope_vendor" in str(exc.orig):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "an active supplier rule for this vendor already exists "
            "in this scope",
        )
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "supplier rule violates a constraint",
    )


@router.get("/supplier-rules")
async def list_supplier_rules(
    request: Request,
    include_inactive: bool = Query(default=False),
    company_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Tenant's supplier rules, newest first; inactive (soft-deleted)
    rules hidden unless requested. No vault gate — rules are pure DB."""
    tenant_id = resolve_tenant_id(request)
    rules, total = await inbox_svc.list_supplier_rules(
        session,
        tenant_id,
        include_inactive=include_inactive,
        company_id=company_id,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return {
        "items": [_shape_rule(r) for r in rules],
        "total": total,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }


@router.post("/supplier-rules", status_code=status.HTTP_201_CREATED)
async def create_supplier_rule(
    request: Request,
    payload: SupplierRuleCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    if payload.company_id is not None:
        await _validate_company(session, payload.company_id, tenant_id)
    try:
        rule = await inbox_svc.create_supplier_rule(
            session,
            tenant_id,
            vendor_name=payload.vendor_name,
            contact_id=payload.contact_id,
            company_id=payload.company_id,
            vendor_abn=payload.vendor_abn,
            account_id=payload.account_id,
            tax_code_id=payload.tax_code_id,
            record_kind=payload.record_kind,
        )
        await session.commit()
    except inbox_svc.SupplierRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise await _rule_integrity_http(session, exc) from exc
    await session.refresh(rule)
    return _shape_rule(rule)


@router.patch("/supplier-rules/{rule_id}")
async def patch_supplier_rule(
    request: Request,
    rule_id: UUID,
    payload: SupplierRulePatch,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Partial update; ``{"active": false}`` is the soft delete,
    ``{"active": true}`` re-activates (409 if the vendor slot has been
    re-taken by a newer active rule)."""
    tenant_id = resolve_tenant_id(request)
    rule = await inbox_svc.get_supplier_rule(session, tenant_id, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "supplier rule not found")
    fields = payload.model_dump(exclude_unset=True)
    if "company_id" in fields and fields["company_id"] is not None:
        await _validate_company(session, fields["company_id"], tenant_id)
    try:
        rule = await inbox_svc.update_supplier_rule(session, rule, fields=fields)
        await session.commit()
    except inbox_svc.SupplierRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise await _rule_integrity_http(session, exc) from exc
    await session.refresh(rule)
    return _shape_rule(rule)


# ---------------------------------------------------------------------------
# Email-in addresses (spec §3/§4 phase 3)
# Dual-gated: the router gate (FLAG_DOCUMENT_INBOX) plus a route-level
# FLAG_INBOX_EMAIL (Business+) — the sync_xero idiom, same as the
# extract-retry endpoint below. No vault gate — addresses are pure DB.
# ---------------------------------------------------------------------------


class EmailAddressCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Default company routing for documents arriving via this address.
    company_id: UUID | None = None


def _shape_email_address(addr: InboxEmailAddress) -> dict[str, Any]:
    domain = settings.inbox_mail_domain.strip().lower()
    return {
        "id": str(addr.id),
        "token": addr.token,
        # The user-facing address — None until the operator configures
        # SAEBOOKS_INBOX_MAIL_DOMAIN (wiring the mailbox is pure config).
        "address": f"{addr.token}@{domain}" if domain else None,
        "company_id": str(addr.company_id) if addr.company_id else None,
        "active": addr.active,
        "revoked_at": _iso(addr.revoked_at),
        "created_at": _iso(addr.created_at),
        "updated_at": _iso(addr.updated_at),
    }


@router.get(
    "/email-addresses",
    dependencies=[Depends(require_feature(FLAG_INBOX_EMAIL))],
)
async def list_email_addresses(
    request: Request,
    include_revoked: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    addresses = await inbox_svc.list_email_addresses(
        session, tenant_id, include_revoked=include_revoked
    )
    return {"items": [_shape_email_address(a) for a in addresses]}


@router.post(
    "/email-addresses",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature(FLAG_INBOX_EMAIL))],
)
async def create_email_address(
    request: Request,
    payload: EmailAddressCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Mint a new ingestion address (server-minted token — the address
    is the credential). Multiple active addresses per tenant, one per
    company on a multi-entity tenant."""
    tenant_id = resolve_tenant_id(request)
    if payload.company_id is not None:
        await _validate_company(session, payload.company_id, tenant_id)
    addr = await inbox_svc.create_email_address(
        session,
        tenant_id,
        company_id=payload.company_id,
        created_by=_user_id_from_request(request),
    )
    await change_log_svc.append(
        session,
        entity="inbox_email_address",
        entity_id=addr.id,
        op="create",
        actor=_actor_from_request(request),
        payload={"company_id": str(payload.company_id) if payload.company_id else None},
        version=1,
        tenant_id=tenant_id,
    )
    await session.commit()
    await session.refresh(addr)
    return _shape_email_address(addr)


@router.post(
    "/email-addresses/{address_id}/revoke",
    dependencies=[Depends(require_feature(FLAG_INBOX_EMAIL))],
)
async def revoke_email_address(
    request: Request,
    address_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kill a (leaked/retired) address. Idempotent; the row stays as
    the audit record and the token stops routing on the next poll."""
    tenant_id = resolve_tenant_id(request)
    addr = await inbox_svc.get_email_address(session, tenant_id, address_id)
    if addr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email address not found")
    inbox_svc.revoke_email_address(addr)
    await change_log_svc.append(
        session,
        entity="inbox_email_address",
        entity_id=addr.id,
        op="revoke",
        actor=_actor_from_request(request),
        payload={"active": False},
        version=1,
        tenant_id=tenant_id,
    )
    await session.commit()
    await session.refresh(addr)
    return _shape_email_address(addr)


# ---------------------------------------------------------------------------
# Manual extraction retry — dual-gated (FLAG_DOCUMENT_INBOX + FLAG_AI_EXTRACTION)
# ---------------------------------------------------------------------------


@router.post(
    "/documents/{document_id}/extract",
    dependencies=[Depends(require_feature(FLAG_AI_EXTRACTION))],
)
async def retry_extraction(
    request: Request,
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-run extraction now (resets the sweep attempt counters).

    Legal from RECEIVED / NEEDS_REVIEW / READY / FAILED; terminal
    documents 409. Extraction is idempotent — a re-run replaces
    ``extract`` wholesale and leaves ``extraction_override`` untouched.
    """
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    doc = await _get_document(session, document_id, tenant_id)

    # Reset the sweep counters (spec §3) before the run; run_extraction
    # then counts this attempt as the first of the fresh series.
    doc.attempt_count = 0
    doc.next_attempt_at = datetime.now(UTC)
    try:
        doc = await inbox_svc.run_extraction(session, doc)
    except inbox_svc.IllegalTransitionError as exc:
        await session.rollback()
        raise _conflict_from(exc) from exc
    return _shape_document(doc)


# ---------------------------------------------------------------------------
# Publish — EXPENSE only (phase 1)
# ---------------------------------------------------------------------------


@router.post("/documents/{document_id}/publish", status_code=status.HTTP_201_CREATED)
async def publish_document(
    request: Request,
    document_id: UUID,
    payload: PublishBody,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Publish the reviewed document as a **DRAFT** record.

    Phase 2: ``record_kind`` accepts EXPENSE, BILL and CREDIT_NOTE
    (anything else 422). The DRAFT record is created through the same
    service functions behind ``POST /api/v1/expenses`` / ``/bills`` /
    ``/credit-notes`` (``api_create`` in each service) — never a manual
    journal entry, never auto-posted. The source blob is vault-linked to
    the new record, supplier-rule learning runs on the confirmed values
    (spec §6), provenance lands in ``change_log``, and the document
    becomes PUBLISHED (terminal, immutable provenance).
    """
    _require_enabled()
    tenant_id = resolve_tenant_id(request)

    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "X-Idempotency-Key header is required on publish",
        )

    raw_body = await request.body()
    claim = await claim_or_fetch(
        session, key, tenant_id, hashlib.sha256(raw_body).hexdigest()
    )
    if claim.status == ClaimStatus.CONFLICT:
        return JSONResponse(
            {
                "code": "idempotency_key_conflict",
                "message": "X-Idempotency-Key reused with a different request body",
            },
            status_code=422,
        )
    if claim.status == ClaimStatus.IN_FLIGHT:
        return JSONResponse(
            {
                "code": "request_in_flight",
                "message": (
                    "A request with this idempotency key is currently being "
                    "processed. Retry after 1 second."
                ),
            },
            status_code=503,
            headers={"Retry-After": "1"},
        )
    if claim.status == ClaimStatus.REPLAY:
        return JSONResponse(
            content=json.loads(claim.response_body) if claim.response_body else {},
            status_code=claim.response_status or 201,
        )

    # FOR UPDATE: serialise concurrent publishes (and publish-vs-reject)
    # on the row — the loser blocks, re-reads the winner's committed
    # status and 409s below instead of creating a second record.
    doc = await _get_document(session, document_id, tenant_id, for_update=True)

    try:
        kind = PublishedRecordKind(payload.record_kind.upper())
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"record_kind '{payload.record_kind}' is not supported; "
            "expected one of EXPENSE, BILL, CREDIT_NOTE",
        ) from exc
    if kind is PublishedRecordKind.EXPENSE and payload.payment_account_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "payment_account_id is required when record_kind is EXPENSE",
        )
    try:
        inbox_svc.ensure_can_transition(doc.status, InboxDocumentStatus.PUBLISHED)
    except inbox_svc.IllegalTransitionError as exc:
        raise _conflict_from(exc) from exc

    await _validate_company(session, payload.company_id, tenant_id)
    # Belt-and-braces contact ownership check. expenses/bills api_create
    # re-validate internally (CIVL-1); credit_notes.api_create does not,
    # so this router-level check keeps the three paths uniform.
    try:
        await inbox_svc.validate_coding_fks(
            session, tenant_id, contact_id=payload.contact_id
        )
    except inbox_svc.SupplierRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    actor = _actor_from_request(request)
    lines = [line.model_dump() for line in payload.lines]

    # Create the DRAFT record through the standard service path — the
    # same api_create behind POST /api/v1/{expenses,bills,credit-notes};
    # never create_journal_entry, never auto-posted. ``commit=False``
    # (the quote→invoice conversion precedent): the idempotency claim,
    # the record, the rule effects, the PUBLISHED stamp, the change_log
    # row and the stored idempotency response all commit in ONE
    # transaction below — any failure rolls the whole publish back, so
    # there is never an orphan DRAFT record and the idempotency key is
    # never left stuck IN_FLIGHT.
    try:
        if kind is PublishedRecordKind.EXPENSE:
            record = await expenses_svc.api_create(
                session,
                payload.company_id,
                tenant_id,
                actor=actor,
                payment_account_id=payload.payment_account_id,
                expense_date=payload.date,
                contact_id=payload.contact_id,
                lines=lines,
                reference=payload.reference,
                notes=payload.notes,
                commit=False,
            )
        elif kind is PublishedRecordKind.BILL:
            record = await bills_svc.api_create(
                session,
                payload.company_id,
                tenant_id,
                actor,
                contact_id=payload.contact_id,
                issue_date=payload.date,
                due_date=payload.due_date,
                lines=lines,
                reference=payload.reference,
                notes=payload.notes,
                commit=False,
            )
        else:  # CREDIT_NOTE
            record = await credit_notes_svc.api_create(
                session,
                payload.company_id,
                tenant_id,
                actor,
                contact_id=payload.contact_id,
                issue_date=payload.date,
                lines=lines,
                reference=payload.reference,
                notes=payload.notes,
                commit=False,
            )
    except (
        ValueError,
        expenses_svc.ExpenseError,
        bills_svc.BillError,
        credit_notes_svc.CreditNoteError,
    ) as exc:
        raise HTTPException(422, str(exc)) from exc

    entity_kind = _PUBLISH_ENTITY_KIND[kind]

    # Supplier-rule bookkeeping + learning on the confirmed values
    # (spec §6). The rule that SUGGESTED (doc.supplier_rule_id) is the
    # provenance fact and is captured before learning can touch anything.
    suggesting_rule_id = doc.supplier_rule_id
    await inbox_svc.apply_publish_rule_effects(
        session,
        doc,
        company_id=payload.company_id,
        record_kind=kind,
        contact_id=payload.contact_id,
        line_account_ids=[line.account_id for line in payload.lines],
        line_tax_code_ids=[line.tax_code_id for line in payload.lines],
        learn_rule=payload.learn_rule,
        update_rule=payload.update_rule,
    )

    # Stamp provenance + terminal status.
    inbox_svc.transition(doc, InboxDocumentStatus.PUBLISHED)
    doc.company_id = payload.company_id
    doc.published_record_kind = kind
    doc.published_record_id = record.id
    doc.published_by = _user_id_from_request(request)
    doc.published_at = datetime.now(UTC)
    doc.version += 1

    await change_log_svc.append(
        session,
        entity="inbox_document",
        entity_id=doc.id,
        op="publish",
        actor=actor,
        payload={
            "published_record_kind": kind.value,
            "published_record_id": str(record.id),
            "extract_model": doc.extract_model,
            "supplier_rule_id": (
                str(suggesting_rule_id) if suggesting_rule_id else None
            ),
            "idempotency_key": key,
            "actor": actor,
            "vault_file_id": str(doc.vault_file_id),
            "source": str(doc.source),
        },
        version=doc.version,
        tenant_id=tenant_id,
    )

    # change_log flushed the UPDATE, expiring server-touched columns —
    # refresh (async) before serialising into the idempotency response.
    await session.refresh(doc)
    body = {
        "document": _shape_document(doc),
        "record": {
            "kind": kind.value,
            "id": str(record.id),
            "status": str(record.status),
        },
    }
    await store_response(session, key, 201, json.dumps(body).encode())
    await session.commit()

    # Attach the source document to the DRAFT record — AFTER the commit
    # so an external vault call never holds the row lock / transaction
    # open, and a rollback can never leave a link pointing at a record
    # that was never created. A vault 409 is idempotent success; any
    # other vault failure is logged but does not unwind the publish —
    # the record exists, the doc still points at the blob, and
    # re-linking is a recoverable operator action.
    try:
        await vault_client.link(
            tenant_id,
            doc.vault_file_id,
            entity_kind=entity_kind,
            entity_id=record.id,
            actor=actor,
        )
    except vault_client.VaultError as exc:
        logger.warning(
            "publish: vault link failed (file=%s %s=%s): %s",
            doc.vault_file_id, entity_kind, record.id, exc,
        )

    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post("/documents/{document_id}/reject")
async def reject_document(
    request: Request,
    document_id: UUID,
    payload: RejectBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Reject a reviewable document. The blob is vault soft-deleted
    (``archived_at`` — never hard-deleted); the row stays as the audit
    record and its content hash frees up for a future re-upload (the
    dedupe unique excludes REJECTED)."""
    _require_enabled()
    tenant_id = resolve_tenant_id(request)
    doc = await _get_document(session, document_id, tenant_id, for_update=True)

    try:
        inbox_svc.ensure_can_transition(doc.status, InboxDocumentStatus.REJECTED)
    except inbox_svc.IllegalTransitionError as exc:
        raise _conflict_from(exc) from exc

    # An emailed byte-duplicate row deliberately reuses the original's
    # blob (no second copy is stored) — if any sibling row still points
    # at this vault file, keep the blob live so the DUPLICATE audit
    # rows' preview/download keep working.
    if await inbox_svc.count_blob_siblings(session, doc) == 0:
        try:
            await vault_client.delete(tenant_id, doc.vault_file_id)
        except vault_client.VaultNotFound:
            pass  # already archived/gone — reject is idempotent on the blob
        except vault_client.VaultError as exc:
            raise _map_vault_error(exc) from exc
    else:
        logger.info(
            "reject: keeping blob %s — shared by sibling inbox rows",
            doc.vault_file_id,
        )

    inbox_svc.transition(doc, InboxDocumentStatus.REJECTED)
    doc.reject_reason = payload.reason
    doc.reject_note = payload.note
    doc.version += 1

    await change_log_svc.append(
        session,
        entity="inbox_document",
        entity_id=doc.id,
        op="update",
        actor=_actor_from_request(request),
        payload=_shape_document(doc),
        version=doc.version,
        tenant_id=tenant_id,
    )
    await session.commit()
    await session.refresh(doc)  # reload server-touched updated_at for the shape
    return _shape_document(doc)
