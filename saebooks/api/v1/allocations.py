"""JSON router — ``/api/v1/allocation_rules``.

Overhead allocation rules for multi-company / multi-site cost sharing.
/allocations previously returned 404.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Feature-gated to Business+ via FLAG_ALLOCATION_RULES.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
* ``POST /{id}/apply`` generates + posts the allocation JE, returning the
  new journal entry id.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    AllocationApplyIn,
    AllocationApplyOut,
    AllocationRuleConflictBody,
    AllocationRuleCreate,
    AllocationRuleListOut,
    AllocationRuleOut,
    AllocationRuleUpdate,
)
from saebooks.services import allocations as svc
from saebooks.services import journal as journal_svc
from saebooks.services.features import FLAG_ALLOCATION_RULES, require_feature
from saebooks.services.hard_delete import hard_delete_with_audit

router = APIRouter(
    prefix="/allocation_rules",
    tags=["allocation_rules"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_ALLOCATION_RULES)),
    ],
)


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _dump(rule: Any) -> dict[str, Any]:
    return json.loads(AllocationRuleOut.model_validate(rule).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=AllocationRuleListOut)
async def list_allocation_rules(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AllocationRuleListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_rules(
        session,
        company_id,
        tenant_id,
        archived=archived,
        limit=page_size,
        offset=offset,
    )
    return AllocationRuleListOut(
        items=[AllocationRuleOut.model_validate(r) for r in items],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{rule_id}", response_model=AllocationRuleOut)
async def get_allocation_rule(
    request: Request,
    rule_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AllocationRuleOut:
    tenant_id = resolve_tenant_id(request)
    rule = await svc.api_get(session, rule_id, tenant_id, company_id=company_id)
    if rule is None:
        raise HTTPException(404, "Allocation rule not found")
    return AllocationRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=AllocationRuleOut)
async def create_allocation_rule(
    request: Request,
    body: AllocationRuleCreate,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AllocationRuleOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "user", "api")
    try:
        rule = await svc.api_create(
            session,
            company_id,
            tenant_id,
            name=body.name,
            description=body.description,
            source_account_id=body.source_account_id,
            targets=[t.model_dump(mode="json") for t in body.targets],
            is_active=body.is_active,
            actor=str(actor),
        )
    except svc.AllocationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return AllocationRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch("/{rule_id}", response_model=AllocationRuleOut)
async def update_allocation_rule(
    request: Request,
    rule_id: UUID,
    body: AllocationRuleUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AllocationRuleOut | JSONResponse:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "user", "api")
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header required for PATCH")

    updates: dict[str, Any] = body.model_dump(exclude_unset=True)
    if "targets" in updates and updates["targets"] is not None:
        updates["targets"] = [
            t.model_dump(mode="json") if hasattr(t, "model_dump") else t
            for t in updates["targets"]
        ]

    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.api_get(session, rule_id, tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Allocation rule not found")

    try:
        rule = await svc.api_update(
            session,
            rule_id,
            tenant_id,
            expected_version=expected,
            actor=str(actor),
            **updates,
        )
    except svc.VersionConflict as exc:
        conflict = AllocationRuleConflictBody(
            current=AllocationRuleOut.model_validate(exc.current)
        )
        return JSONResponse(
            status_code=409,
            content=json.loads(conflict.model_dump_json()),
        )
    except svc.AllocationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return AllocationRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# Delete (soft-archive)
# ---------------------------------------------------------------------------


@router.delete("/{rule_id}", status_code=204)
async def delete_allocation_rule(
    request: Request,
    rule_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "user", "api")
    if hard:
        existing = await svc.api_get(session, rule_id, tenant_id, company_id=company_id)
        if existing is None:
            raise HTTPException(404, "Allocation rule not found")
        await hard_delete_with_audit(
            session, existing, "allocation_rules", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header required for DELETE")
    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.api_get(session, rule_id, tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Allocation rule not found")
    try:
        await svc.api_delete(
            session,
            rule_id,
            tenant_id,
            expected_version=expected,
            actor=str(actor),
        )
    except svc.VersionConflict as exc:
        rule = exc.current
        conflict = AllocationRuleConflictBody(
            current=AllocationRuleOut.model_validate(rule)
        )
        return JSONResponse(
            status_code=409,
            content=json.loads(conflict.model_dump_json()),
        )
    except svc.AllocationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Apply — generate + post the allocation JE
# ---------------------------------------------------------------------------


@router.post("/{rule_id}/apply", status_code=201, response_model=AllocationApplyOut)
async def apply_allocation_rule(
    request: Request,
    rule_id: UUID,
    body: AllocationApplyIn,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> AllocationApplyOut:
    """Generate and post a journal entry for this allocation rule.

    Debits each target account for its share and credits the source
    account for the full amount. The entry is immediately posted.
    """
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "user", "api")
    rule = await svc.api_get(session, rule_id, tenant_id, company_id=company_id)
    if rule is None:
        raise HTTPException(404, "Allocation rule not found")
    if rule.archived_at is not None:
        raise HTTPException(422, "Cannot apply an archived allocation rule")

    try:
        lines = svc.compute_allocation_lines(
            rule,
            Decimal(str(body.amount)),
            description=body.description,
        )
    except svc.AllocationError as exc:
        raise HTTPException(422, str(exc)) from exc

    description = body.description or f"Allocation: {rule.name}"
    try:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            entry_date=body.entry_date,
            description=description,
            lines=lines,  # type: ignore[arg-type]
            tenant_id=tenant_id,
        )
        entry = await journal_svc.post(
            session,
            entry.id,
            posted_by=str(actor),
            tenant_id=tenant_id,
        )
    except journal_svc.PostingError as exc:
        raise HTTPException(422, str(exc)) from exc

    return AllocationApplyOut(
        journal_entry_id=entry.id,
        lines_count=len(entry.lines),
        total_amount=body.amount,
    )
