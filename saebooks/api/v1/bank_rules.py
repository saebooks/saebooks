"""JSON router — ``/api/v1/bank_rules``.

Phase 1 cycle 41.

Bank rules auto-categorise imported bank statement lines when the line's
description satisfies the rule's match pattern.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* No optimistic locking — ``BankRule`` has no ``version`` column.
* No soft-delete — ``BankRule`` has no ``archived_at``; DELETE is hard.
* Multi-tenant: all queries filter by company_id from the shared
  ``get_active_company_id`` dep (honours ``X-Company-Id``; falls back
  to the first active company for the tenant).
* Extra endpoints:
  - ``POST /api/v1/bank_rules/apply`` — apply ALL active auto_create rules
    to all unmatched lines; returns ``{"applied": N}``.
  - ``POST /api/v1/bank_rules/{id}/apply`` — apply one rule to all matching
    unmatched lines; returns ``{"applied": N}``.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    BankRuleApplyOut,
    BankRuleCreate,
    BankRuleListOut,
    BankRuleOut,
    BankRuleUpdate,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.models.bank_rule import BankRule, MatchType
from saebooks.services import bank_rules as svc
from saebooks.services.hard_delete import hard_delete_with_audit

router = APIRouter(
    prefix="/bank_rules",
    tags=["bank_rules"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dump(rule: BankRule) -> dict[str, Any]:
    return json.loads(BankRuleOut.model_validate(rule).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BankRuleListOut)
async def list_bank_rules(
    active_only: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankRuleListOut:
    count_stmt = (
        select(func.count())
        .select_from(BankRule)
        .where(BankRule.company_id == company_id)
    )
    if active_only:
        count_stmt = count_stmt.where(BankRule.is_active.is_(True))
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(BankRule)
        .where(BankRule.company_id == company_id)
        .order_by(BankRule.priority.desc(), BankRule.name)
        .offset(offset)
        .limit(limit)
    )
    if active_only:
        stmt = stmt.where(BankRule.is_active.is_(True))
    rules = list((await session.execute(stmt)).scalars().all())

    return BankRuleListOut(
        items=[BankRuleOut.model_validate(r) for r in rules],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{rule_id}", response_model=BankRuleOut)
async def get_bank_rule(
    rule_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankRuleOut:
    rule = await svc.get(session, rule_id)
    if rule is None or rule.company_id != company_id:
        raise HTTPException(404, "Bank rule not found")
    return BankRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BankRuleOut, status_code=201)
async def create_bank_rule(
    payload: BankRuleCreate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    try:
        match_type = MatchType(payload.match_type.upper())
    except ValueError:
        valid = ", ".join(m.value for m in MatchType)
        raise HTTPException(422, f"Invalid match_type '{payload.match_type}'. Valid values: {valid}") from None
    try:
        rule = await svc.create(
            session,
            company_id,
            name=payload.name,
            match_pattern=payload.match_pattern,
            match_type=match_type,
            account_id=payload.account_id,
            tax_code=payload.tax_code,
            contact_id=payload.contact_id,
            description_template=payload.description_template,
            auto_create=payload.auto_create,
            priority=payload.priority,
            is_active=payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(rule)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH — no If-Match; BankRule has no version column)
# ---------------------------------------------------------------------------


@router.patch("/{rule_id}", response_model=BankRuleOut)
async def update_bank_rule(
    rule_id: UUID,
    payload: BankRuleUpdate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    rule = await svc.get(session, rule_id)
    if rule is None or rule.company_id != company_id:
        raise HTTPException(404, "Bank rule not found")

    kwargs = payload.model_dump(exclude_unset=True)

    # Convert match_type string to enum if present
    if "match_type" in kwargs and kwargs["match_type"] is not None:
        try:
            kwargs["match_type"] = MatchType(kwargs["match_type"].upper())
        except ValueError:
            valid = ", ".join(m.value for m in MatchType)
            raise HTTPException(422, f"Invalid match_type. Valid values: {valid}") from None

    try:
        updated = await svc.update(
            session,
            rule_id,
            performed_by=f"api:{bearer[:8]}…",
            **kwargs,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(updated)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (hard delete — BankRule has no archived_at → 204)
# ---------------------------------------------------------------------------


@router.delete("/{rule_id}", responses={204: {"description": "Deleted"}})
async def delete_bank_rule(
    request: Request,
    rule_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    rule = await svc.get(session, rule_id)
    if rule is None or rule.company_id != company_id:
        raise HTTPException(404, "Bank rule not found")

    if hard:
        await hard_delete_with_audit(
            session, rule, "bank_rules", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    await svc.delete(
        session,
        rule_id,
        performed_by=f"api:{bearer[:8]}…",
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Apply all — bulk trigger for auto_create rules
# NOTE: must be registered before /{rule_id}/apply to avoid routing clash.
# ---------------------------------------------------------------------------


@router.post("/apply", response_model=BankRuleApplyOut)
async def apply_all_bank_rules(
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Apply all active auto_create rules to unmatched bank statement lines.

    Returns ``{"applied": N}`` where N is the number of lines matched
    and journal entries created.
    """
    counts = await svc.auto_apply_rules(session, company_id)
    return BankRuleApplyOut(applied=counts.get("matched", 0))


# ---------------------------------------------------------------------------
# Apply single rule
# ---------------------------------------------------------------------------


@router.post("/{rule_id}/apply", response_model=BankRuleApplyOut)
async def apply_single_bank_rule(
    rule_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Apply one rule to all matching unmatched bank statement lines.

    Returns ``{"applied": N}`` where N is the number of lines matched
    and journal entries created.
    """
    rule = await svc.get(session, rule_id)
    if rule is None or rule.company_id != company_id:
        raise HTTPException(404, "Bank rule not found")

    # Find all unmatched lines that match this rule
    matching_lines = await svc.preview_matches(
        session, company_id, rule, limit=10000
    )

    applied = 0
    for line in matching_lines:
        try:
            await svc.apply_rule_to_line(
                session,
                line.id,
                rule_id,
                posted_by=f"api:{bearer[:8]}…",
            )
            applied += 1
        except Exception:
            # Line may have been matched by a concurrent request; skip it
            pass

    return BankRuleApplyOut(applied=applied)
