"""JSON router — ``/api/v1/journal_entries``.

Phase 1 tier-3 general ledger endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at) returning 204.
* Lines are nested in the response.
* Status transitions go through dedicated endpoints:
    POST /{id}/post    — DRAFT → POSTED
    POST /{id}/reverse — POSTED → REVERSED (creates mirror reversal entry)
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on transition endpoints.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; every query is gated by the ``tenant_isolation`` RLS
policy from migration 0055. Existence checks pass ``tenant_id`` to
``svc.get`` so a foreign-tenant UUID returns 404 even if the caller
knows the id.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_active_user_id, get_session
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    JournalEntryConflictBody,
    JournalEntryCreate,
    JournalEntryListOut,
    JournalEntryOut,
    JournalEntryPostBody,
    JournalEntryReverseBody,
    JournalEntryUpdate,
    ReviewFlagBody,
)
from saebooks.models.journal import EntryStatus
from saebooks.services import journal_entries as svc
from saebooks.services import review_flags as review_flags_svc
from saebooks.services.authz import no_additional_gate, require_permission_or_role
from saebooks.services.features import FLAG_EXTENDED_AUDIT_MODES, feature_enabled_for_request
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/journal_entries",
    tags=["journal_entries"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
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


def _dump(entry: Any) -> dict[str, Any]:
    return json.loads(JournalEntryOut.model_validate(entry).model_dump_json())


def _resolve_actor_role(request: Request) -> str | None:
    """Resolve the calling user's role for the F-04 period-lock gate.

    Priority order:
      1. ``request.state.role`` stamped by ``require_bearer`` after JWT
         decode — the canonical path for user-authenticated requests.
      2. ``X-Actor-Role`` request header — escape hatch for service-to-
         service calls where the upstream has already enforced authn.
         Only honoured when no JWT user is on the request (so a normal
         user can't elevate themselves by sending the header).
      3. ``admin`` when the request came in on the static dev bearer
         (``SAEBOOKS_DEV_API_TOKEN``). The dev bearer is a god-key used
         by scripts and the test suite; treating it as admin preserves
         backward compatibility without weakening the gate for normal
         user JWTs.
      4. ``None`` otherwise — fail-closed at the service layer.
    """
    role = getattr(request.state, "role", None)
    if role:
        return str(role)

    # No JWT user on the request — check the explicit header escape hatch.
    hdr = request.headers.get("x-actor-role")
    if hdr:
        return hdr.strip()

    # Static dev bearer path — require_bearer stamped jwt_claims but no
    # user/role. Treat as admin for backward compat with scripts/tests
    # that rely on SAEBOOKS_DEV_API_TOKEN. The user JWT path always
    # populates state.role above, so a normal user can't reach here with
    # an elevated role.
    if getattr(request.state, "jwt_claims", None) and not getattr(
        request.state, "user", None
    ):
        return "admin"

    return None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=JournalEntryListOut)
async def list_journal_entries(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status: str | None = Query(default=None),
    ref: str | None = Query(default=None, description="Case-insensitive substring on journal_entries.ref"),
    description: str | None = Query(default=None, description="Case-insensitive substring on description"),
    posted_by: str | None = Query(default=None, description="Case-insensitive substring on posted_by"),
    account_id: UUID | None = Query(default=None, description="Only entries with a line on this account"),
    account_code: str | None = Query(default=None, description="Convenience: resolves to account_id by Account.code"),
    flagged: bool | None = Query(default=None, description="Filter by flagged_for_review"),
    sort: str = Query(default="date", description="Sort column: date | ref | total_debit | status"),
    dir: str = Query(default="desc", description="Sort direction: asc | desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalEntryListOut:
    offset = (page - 1) * page_size
    status_enum: EntryStatus | None = None
    if status is not None:
        try:
            status_enum = EntryStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    if sort not in svc.SORTABLE_FIELDS:
        raise HTTPException(
            400,
            f"Invalid sort '{sort}' — must be one of {list(svc.SORTABLE_FIELDS)}",
        )
    dir_lower = (dir or "").lower()
    if dir_lower not in ("asc", "desc"):
        raise HTTPException(400, f"Invalid dir '{dir}' — must be asc or desc")

    tenant_id = resolve_tenant_id(request)
    entries, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        date_from=date_from,
        date_to=date_to,
        status=status_enum,
        ref=ref,
        description=description,
        posted_by=posted_by,
        account_id=account_id,
        account_code=account_code,
        flagged=flagged,
        sort_field=sort,
        sort_dir=dir_lower,
        limit=page_size,
        offset=offset,
    )
    return JournalEntryListOut(
        items=[JournalEntryOut.model_validate(e) for e in entries],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /{id}/review-flag — Gap 3 (set/clear flag for review)
# ---------------------------------------------------------------------------


@router.post("/{entry_id}/review-flag", response_model=JournalEntryOut)
async def set_journal_entry_review_flag(
    entry_id: UUID,
    payload: ReviewFlagBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalEntryOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        await review_flags_svc.set_review_flag(
            session,
            "journal_entry",
            entry_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=str(actor),
            flagged=payload.flagged,
            review_note=payload.review_note,
        )
    except review_flags_svc.ReviewFlagError as exc:
        raise HTTPException(404, str(exc)) from exc
    entry = await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id)
    return JournalEntryOut.model_validate(entry)


# ---------------------------------------------------------------------------
# Filter options — populates dropdowns on the web list page
# ---------------------------------------------------------------------------


@router.get("/_filter_options")
async def journal_entry_filter_options(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> dict[str, list[str]]:
    """Return values for populating the JE list filter dropdowns.

    * ``posted_by`` — distinct non-null posted_by values, alphabetical
    * ``ref_prefixes`` — distinct ref prefixes (chars before first digit run),
      top 30 by frequency, then alphabetical

    All results are RLS-gated via the existing tenant binding on the
    session connection.
    """
    from sqlalchemy import text

    posted_by_rows = (await session.execute(
        text(
            "SELECT DISTINCT posted_by FROM journal_entries "
            "WHERE company_id = :cid AND archived_at IS NULL "
            "AND posted_by IS NOT NULL AND posted_by <> '' "
            "ORDER BY posted_by"
        ),
        {"cid": str(company_id)},
    )).all()

    # Top 30 ref prefixes by occurrence — strip leading alpha/punct sequence
    # up to the first digit run. e.g. CLEANUP-1110-0001 → CLEANUP-, EX1268 → EX,
    # auto-fxfee-2026-05-23 → auto-fxfee-, JRN7030 → JRN.
    prefix_rows = (await session.execute(
        text(
            "SELECT prefix FROM ("
            "  SELECT regexp_replace(ref, '\\d.*$', '') AS prefix, count(*) AS c "
            "  FROM journal_entries "
            "  WHERE company_id = :cid AND archived_at IS NULL "
            "  GROUP BY prefix "
            "  HAVING regexp_replace(ref, '\\d.*$', '') <> '' "
            "  ORDER BY c DESC, prefix ASC "
            "  LIMIT 30"
            ") sub ORDER BY prefix"
        ),
        {"cid": str(company_id)},
    )).all()

    return {
        "posted_by": [r[0] for r in posted_by_rows],
        "ref_prefixes": [r[0] for r in prefix_rows],
    }


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{entry_id}", response_model=JournalEntryOut)
async def get_journal_entry(
    entry_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JournalEntryOut:
    tenant_id = resolve_tenant_id(request)
    entry = await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id)
    if entry is None:
        raise HTTPException(404, "Journal entry not found")
    # Populate source_type/source_id by reverse-lookup (#27, approach b).
    # One extra query per GET; the durable (a) approach would store these
    # on the JE row but requires touching invoices/bills/payments write
    # paths owned by other agents. Documented choice — approach (b).
    src = await svc.get_source_doc(session, entry_id, tenant_id=tenant_id)
    out = JournalEntryOut.model_validate(entry)
    if src is not None:
        out.source_type = src["type"]
        out.source_id = src["id"]
    return out


@router.get('/{entry_id}/source')
async def get_journal_entry_source(
    entry_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> dict[str, str | None]:
    """Return the source document linked to this JE, or nulls.

    The five candidate tables (invoices, bills, credit_notes, expenses,
    payments) each hold a journal_entry_id FK; the first hit (by table
    priority order) is returned.
    """
    tenant_id = resolve_tenant_id(request)
    entry = await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id)
    if entry is None:
        raise HTTPException(404, 'Journal entry not found')
    src = await svc.get_source_doc(session, entry_id, tenant_id=tenant_id)
    if src is None:
        return {'type': None, 'id': None, 'ref': None}
    return src


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=JournalEntryOut, status_code=201)
async def create_journal_entry(
    payload: JournalEntryCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    try:
        entry = await svc.create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            entry_date=payload.entry_date,
            narration=payload.narration,
            reference=payload.reference,
            lines=[line.model_dump() for line in payload.lines],
        )
    except (ValueError, svc.JournalEntryError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(entry)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{entry_id}",
    responses={
        200: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
)
async def update_journal_entry(
    entry_id: UUID,
    payload: JournalEntryUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Journal entry not found")

    try:
        lines_data = (
            [line.model_dump() for line in payload.lines]
            if payload.lines is not None
            else None
        )
        entry = await svc.update(
            session,
            entry_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            entry_date=payload.entry_date,
            narration=payload.narration,
            reference=payload.reference,
            status=payload.status,
            lines=lines_data,
            override_reason=payload.override_reason or None,
            actor_role=_resolve_actor_role(request),
            extended_audit_modes_entitled=feature_enabled_for_request(
                FLAG_EXTENDED_AUDIT_MODES, request
            ),
            performed_by=f"api:{bearer[:8]}…",
        )
    except svc.VersionConflict as exc:
        body = JournalEntryConflictBody(
            detail="version mismatch",
            current=JournalEntryOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.JournalEntryError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(entry)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{entry_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
    # No dedicated "journal.void" catalogue code exists (the draft's
    # journal section only covers view/draft/post/reverse) — mapped to
    # the closest destructive-class code, journal.reverse, matching
    # the conservative-not-permissive direction used across this pass.
    dependencies=[
        Depends(require_permission_or_role("journal.reverse", no_additional_gate))
    ],
)
async def void_journal_entry(
    entry_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Journal entry not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "journal_entries", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    try:
        await svc.void(
            session,
            entry_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = JournalEntryConflictBody(
            detail="version mismatch",
            current=JournalEntryOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.JournalEntryError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{entry_id}/post",
    responses={
        200: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
    dependencies=[
        Depends(require_permission_or_role("journal.post", no_additional_gate))
    ],
)
async def post_journal_entry(
    entry_id: UUID,
    request: Request,
    payload: JournalEntryPostBody = Body(default_factory=JournalEntryPostBody),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition journal entry DRAFT → POSTED.

    Checks period lock, auto-posts GST lines, verifies balance.
    Returns 422 if the entry is already POSTED or REVERSED.
    Returns 422 with "Period is locked" if entry_date falls in a locked period
    and no override_reason is supplied in the request body.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    idem_key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if idem_key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, idem_key, tenant_id, body_sha256)
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

    if await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Journal entry not found")

    try:
        entry = await svc.api_post(
            session,
            entry_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            override_reason=payload.override_reason or None,
            actor_role=_resolve_actor_role(request),
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = JournalEntryConflictBody(
            detail="version mismatch",
            current=JournalEntryOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.JournalEntryError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(entry)
    if idem_key is not None:
        await store_response(session, idem_key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Reverse (POST /{id}/reverse → creates new reversal JE, marks original REVERSED)
# ---------------------------------------------------------------------------


@router.post(
    "/{entry_id}/reverse",
    responses={
        201: {"model": JournalEntryOut},
        409: {"model": JournalEntryConflictBody, "description": "Version mismatch"},
    },
    status_code=201,
    dependencies=[
        Depends(require_permission_or_role("journal.reverse", no_additional_gate))
    ],
)
async def reverse_journal_entry(
    entry_id: UUID,
    request: Request,
    payload: JournalEntryReverseBody = Body(default_factory=JournalEntryReverseBody),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Create a reversal of a POSTED journal entry (POSTED → REVERSED).

    Creates a new JournalEntry with all debit/credit lines swapped,
    auto-posts it, and marks the original entry as REVERSED. The new
    reversal entry is returned. Only POSTED entries can be reversed;
    returns 422 for any other status.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with entry version is required")

    idem_key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if idem_key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, idem_key, tenant_id, body_sha256)
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

    if await svc.get(session, entry_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Journal entry not found")

    try:
        reversal = await svc.api_reverse(
            session,
            entry_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            actor_role=_resolve_actor_role(request),
            reversal_date=payload.reversal_date,
            override_reason=payload.override_reason or None,
        )
    except svc.VersionConflict as exc:
        body = JournalEntryConflictBody(
            detail="version mismatch",
            current=JournalEntryOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.JournalEntryError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(reversal)
    if idem_key is not None:
        await store_response(session, idem_key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)
