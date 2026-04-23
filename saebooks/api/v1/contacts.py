"""Pure JSON contacts router — ``/api/v1/contacts``.

Implements the Phase 0 scaffolding pattern that Phase 1 will apply to
every other entity:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` (TODO: JWT in Phase 1).
* Optimistic locking via ``If-Match: <version>`` header on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` header — replayed
  requests return the cached response body + status without re-executing.
* Every write appends a row to ``change_log`` (handled by the service
  layer, not the router).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    ConflictBody,
    ContactCreate,
    ContactListOut,
    ContactOut,
    ContactUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import contacts as svc

router = APIRouter(
    prefix="/contacts",
    tags=["contacts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession) -> UUID:
    """Community edition: all rows belong to the single company. Phase 0
    doesn't wire tenant resolution through the bearer token yet — the
    portal JWT in Phase 1 will carry ``company_id`` as a claim."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    """Parse the ``If-Match`` header as a version integer.

    Accepts ``"5"`` (RFC 7232 weak-etag style) or ``5``. Missing header
    returns ``None`` (no precondition).
    """
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(
    session: AsyncSession, key: UUID
) -> JSONResponse | None:
    """Return the cached response if the idempotency key has already been used."""
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(
        content=existing.response_body,
        status_code=existing.response_status,
    )


async def _remember_idempotent(
    session: AsyncSession,
    key: UUID,
    body: dict[str, Any],
    status_code: int,
) -> None:
    """Record this response under ``key`` so future retries replay it."""
    row = IdempotencyKey(
        key=key,
        response_body=body,
        response_status=status_code,
    )
    session.add(row)
    await session.flush()


def _dump(contact: Contact) -> dict[str, Any]:
    """Pydantic-serialise a Contact row. Uses ``mode='json'`` to get
    stringified UUIDs/datetimes so the dict round-trips through JSONB."""
    return json.loads(ContactOut.model_validate(contact).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ContactListOut)
async def list_contacts(
    contact_type: ContactType | None = Query(default=None, alias="type"),  # noqa: B008
    search: str | None = Query(default=None, alias="q"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ContactListOut:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
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
async def get_contact(contact_id: UUID) -> ContactOut:
    async with AsyncSessionLocal() as session:
        contact = await svc.get(session, contact_id)
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
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
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
            await _remember_idempotent(session, key, body, 201)
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
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

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
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except ValueError as exc:
            # Not-found or bad field — service raises ValueError.
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        await session.refresh(contact)
        body = _dump(contact)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
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
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay
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
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        if contact is None:
            raise HTTPException(404, "Contact not found")
        if key is not None:
            # 204 has no body, but we still record the fact for replay.
            await _remember_idempotent(session, key, {"archived": str(contact.id)}, 204)
            await session.commit()
    return Response(status_code=204)
