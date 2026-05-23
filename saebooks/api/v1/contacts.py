"""Pure JSON contacts router — ``/api/v1/contacts``.

Implements the Phase 0 scaffolding pattern that Phase 1 will apply to
every other entity:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` header on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` header — replayed
  requests return the cached response body + status without re-executing.
* Every write appends a row to ``change_log`` (handled by the service
  layer, not the router).

P0 cross-tenant leak fix
------------------------
This router is the leak's epicentre and the first to migrate to the
shared ``get_session`` dep — see ``saebooks.api.v1.deps``. Behaviour
changes:

* ``_first_company_id`` now scopes by the request tenant, not "the
  oldest active company in the entire DB".
* ``get_contact`` now passes the request tenant to ``svc.get`` so a
  detail lookup for a foreign-tenant UUID returns 404 even if the
  caller knows the UUID.
* Every handler accepts a single ``Depends(get_session)`` session
  with ``app.current_tenant`` set, so the FORCE-RLS policy gates
  every query in the handler.

Idempotency migration (audit-trail #10)
----------------------------------------
Replaced the race-unsafe ``_idempotent_replay`` / ``_remember_idempotent``
helpers (against legacy ``idempotency_keys`` table) with the race-safe
``claim_or_fetch`` / ``store_response`` service (``idempotency_records``
table).  SHA-256 of the raw request body is passed so conflicting bodies
on the same key return HTTP 422 per RFC 8417 §2.1.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    ConflictBody,
    ContactCreate,
    ContactListOut,
    ContactOut,
    ContactUpdate,
    OneOffBulkTagRequest,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import contacts as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/contacts",
    tags=["contacts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession, tenant_id: UUID) -> UUID:
    """Resolve the active company for the request tenant.

    Pre-fix this took no tenant input and returned the oldest active
    company in the entire DB — leaking every authenticated request
    into Default Company. Now scoped by ``tenant_id``: returns the
    oldest active company that belongs to the caller's tenant.
    """
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    """Parse the ``If-Match`` header as a version integer."""
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(contact: Contact) -> dict[str, Any]:
    """Pydantic-serialise a Contact row."""
    return json.loads(ContactOut.model_validate(contact).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ContactListOut)
async def list_contacts(
    request: Request,
    contact_type: ContactType | None = Query(default=None, alias="type"),  # noqa: B008
    search: str | None = Query(default=None, alias="q"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ContactListOut:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    # Count (matches filter minus limit/offset).
    count_stmt = (
        select(func.count())
        .select_from(Contact)
        .where(Contact.company_id == company_id, Contact.archived_at.is_(None))
    )
    if contact_type is not None:
        count_stmt = count_stmt.where(Contact.contact_type == contact_type)
    if search:
        pattern = f"%{search}%"
        count_stmt = count_stmt.where(
            Contact.name.ilike(pattern) | Contact.email.ilike(pattern)
        )
    total = (await session.execute(count_stmt)).scalar_one()
    items = await svc.list_active(
        session,
        company_id,
        contact_type=contact_type,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ContactListOut(
        items=[ContactOut.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    tenant_id = resolve_tenant_id(request)
    contact = await svc.get(session, contact_id, tenant_id=tenant_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    return ContactOut.model_validate(contact)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ContactOut,
    status_code=201,
)
async def create_contact(
    payload: ContactCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )
        # CLAIMED — fall through to write

    company_id = await _first_company_id(session, tenant_id)
    try:
        contact = await svc.create(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
            **payload.model_dump(exclude_unset=False, exclude={"bank_bsb", "bank_account_number", "bank_account_title"}),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # svc.create already committed; refresh inside the session before
    # we dump to dodge a MissingGreenlet when pydantic walks the row.
    await session.refresh(contact)
    body = _dump(contact)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{contact_id}",
    responses={
        200: {"model": ContactOut},
        409: {"model": ConflictBody, "description": "Version mismatch"},
    },
)
async def update_contact(
    contact_id: UUID,
    payload: ContactUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
    key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    # Belt-and-braces: check the contact exists and is owned by this
    # tenant before we attempt the update. RLS already enforces this,
    # but the service-layer ValueError message is friendlier than a
    # silent zero-rows update.
    if await svc.get(session, contact_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Contact not found")

    try:
        contact = await svc.update(
            session,
            contact_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = ConflictBody(
            detail="version mismatch",
            current=ContactOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(contact)
    body = _dump(contact)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive)
# ---------------------------------------------------------------------------


@router.delete(
    "/{contact_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": ConflictBody, "description": "Version mismatch"},
    },
)
async def archive_contact(
    contact_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.get(session, contact_id, tenant_id=tenant_id)
        if existing is None:
            raise HTTPException(404, "Contact not found")
        await hard_delete_with_audit(
            session, existing, "contacts", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 204,
            )

    if await svc.get(session, contact_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Contact not found")

    try:
        contact = await svc.archive(
            session,
            contact_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = ConflictBody(
            detail="version mismatch",
            current=ContactOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    if key is not None:
        archived_body = json.dumps({"archived": str(contact.id)}).encode()
        await store_response(session, key, 204, archived_body)
        await session.commit()
    return Response(status_code=204)

# ---------------------------------------------------------------------------
# Bulk-tag one-off — flip ``is_one_off`` on many contacts in one call.
# ---------------------------------------------------------------------------


@router.post(
    "/bulk-tag-one-off",
    response_model=None,
)
async def bulk_tag_one_off(
    payload: OneOffBulkTagRequest,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Set ``is_one_off`` on a list of contacts in one transaction.

    Body: ``{"contact_ids": [...], "is_one_off": true|false}``.

    Each contact is fetched (tenant-scoped, archived rows skipped), its
    ``is_one_off`` flag updated to the requested value if it differs,
    a change_log row appended, and the version bumped. Already-correct
    rows are skipped silently. Foreign-tenant ids (or non-existent ids)
    are silently skipped — same shape as the existing per-row update
    would yield a 404 individually.

    Returns ``{"flipped": <count>}`` — number of rows whose flag actually
    changed.
    """
    tenant_id = resolve_tenant_id(request)
    flipped = 0
    for cid in payload.contact_ids:
        existing = await svc.get(session, cid, tenant_id=tenant_id)
        if existing is None or existing.archived_at is not None:
            continue
        if existing.is_one_off == payload.is_one_off:
            continue
        try:
            await svc.update(
                session,
                cid,
                actor=f"api:{bearer[:8]}…",
                tenant_id=tenant_id,
                is_one_off=payload.is_one_off,
            )
        except (ValueError, svc.VersionConflict):
            # Skip a bad/conflicting row; the rest of the batch still proceeds.
            continue
        flipped += 1
    return JSONResponse({"flipped": flipped}, status_code=200)

