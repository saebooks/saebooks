"""Allocation rules service — overhead cost-pool distribution.

An allocation rule describes how to split a shared cost pool (the
``source_account``) across target accounts. Calling :func:`apply_rule`
generates a balanced journal entry: credit source, debit each target
for its percentage share of ``amount``.

API-tier functions (``api_create``, ``api_update``, ``api_delete``,
``list_rules``, ``api_get``, ``api_apply``) follow the standard
optimistic-locking + change_log pattern used by budgets, projects, etc.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.allocation_rule import AllocationRule
from saebooks.money import round_money
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_RULE_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "name",
    "description",
    "source_account_id",
    "targets",
    "is_active",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AllocationError(ValueError):
    """Raised on allocation validation or state-transition failure."""


class VersionConflict(Exception):
    def __init__(self, current: AllocationRule) -> None:
        super().__init__(
            f"AllocationRule {current.id} is at version {current.version}"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise(rule: AllocationRule) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in _RULE_COLUMNS:
        val = getattr(rule, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime) or hasattr(val, "isoformat"):
            val = val.isoformat()
        data[key] = val
    return data


def _validate_targets(targets: list[dict[str, Any]]) -> None:
    """Raise AllocationError when targets are malformed or don't sum to 100."""
    if not targets:
        raise AllocationError("At least one target is required")
    total = Decimal("0")
    for i, t in enumerate(targets):
        pct = t.get("percentage")
        if pct is None:
            raise AllocationError(f"Target {i} missing percentage")
        try:
            pct_dec = Decimal(str(pct))
        except Exception as exc:
            raise AllocationError(f"Target {i} percentage is not a number") from exc
        if pct_dec <= 0:
            raise AllocationError(f"Target {i} percentage must be positive")
        if not t.get("account_id"):
            raise AllocationError(f"Target {i} missing account_id")
        total += pct_dec
    if abs(total - Decimal("100")) > Decimal("0.01"):
        raise AllocationError(
            f"Target percentages must sum to 100 (got {total})"
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_rules(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    archived: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[AllocationRule], int]:
    stmt = select(AllocationRule).where(
        AllocationRule.company_id == company_id,
        AllocationRule.tenant_id == tenant_id,
    )
    if not archived:
        stmt = stmt.where(AllocationRule.archived_at.is_(None))
    count_stmt = select(sa_func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AllocationRule.name).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def api_get(
    session: AsyncSession,
    rule_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    company_id: uuid.UUID | None = None,
) -> AllocationRule | None:
    """Fetch a single allocation rule.

    ``company_id`` is the cross-company isolation guard (Layer 2 fix,
    2026-05-24): when supplied, a sibling-company id within the same
    tenant returns ``None``.
    """
    clauses = [
        AllocationRule.id == rule_id,
        AllocationRule.tenant_id == tenant_id,
    ]
    if company_id is not None:
        clauses.append(AllocationRule.company_id == company_id)
    result = await session.execute(
        select(AllocationRule).where(*clauses)
    )
    return result.scalars().first()


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    name: str,
    description: str | None,
    source_account_id: uuid.UUID,
    targets: list[dict[str, Any]],
    is_active: bool = True,
    actor: str = "system",
) -> AllocationRule:
    _validate_targets(targets)
    rule = AllocationRule(
        company_id=company_id,
        tenant_id=tenant_id,
        name=name.strip(),
        description=description,
        source_account_id=source_account_id,
        targets=targets,
        is_active=is_active,
    )
    session.add(rule)
    await session.flush()
    await change_log_svc.append(
        session,
        entity="allocation_rule",
        entity_id=rule.id,
        op="create",
        actor=actor,
        payload=_serialise(rule),
        version=rule.version,
    )
    await session.commit()
    await session.refresh(rule)
    return rule


async def api_update(
    session: AsyncSession,
    rule_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    expected_version: int,
    actor: str = "system",
    **kwargs: Any,
) -> AllocationRule:
    rule = await api_get(session, rule_id, tenant_id)
    if rule is None:
        raise AllocationError(f"AllocationRule {rule_id} not found")
    if rule.version != expected_version:
        raise VersionConflict(rule)
    if rule.archived_at is not None:
        raise AllocationError("Cannot update an archived allocation rule")

    targets = kwargs.get("targets")
    if targets is not None:
        _validate_targets(targets)

    before = _serialise(rule)
    allowed = {"name", "description", "source_account_id", "targets", "is_active"}
    for key, val in kwargs.items():
        if key not in allowed:
            raise AllocationError(f"Unknown field: {key!r}")
        setattr(rule, key, val)
    rule.version += 1
    rule.updated_at = datetime.now(UTC)

    await change_log_svc.append(
        session,
        entity="allocation_rule",
        entity_id=rule.id,
        op="update",
        actor=actor,
        payload={"before": before, "after": _serialise(rule)},
        version=rule.version,
    )
    await session.commit()
    await session.refresh(rule)
    return rule


async def api_delete(
    session: AsyncSession,
    rule_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    expected_version: int,
    actor: str = "system",
) -> None:
    rule = await api_get(session, rule_id, tenant_id)
    if rule is None:
        raise AllocationError(f"AllocationRule {rule_id} not found")
    if rule.version != expected_version:
        raise VersionConflict(rule)
    before = _serialise(rule)
    rule.archived_at = datetime.now(UTC)
    rule.version += 1
    await change_log_svc.append(
        session,
        entity="allocation_rule",
        entity_id=rule.id,
        op="archive",
        actor=actor,
        payload={"before": before},
        version=rule.version,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Apply — generate balanced journal lines
# ---------------------------------------------------------------------------


def compute_allocation_lines(
    rule: AllocationRule,
    amount: Decimal,
    *,
    description: str | None = None,
) -> list[dict[str, Any]]:
    """Return the unsaved journal lines for applying ``rule`` to ``amount``.

    Returns a list of dicts ready to be passed to ``services.journal``.
    Lines are:
    - one CREDIT on ``source_account_id`` for the full amount
    - one DEBIT per target for its percentage share

    Rounding: each target is rounded to 2 d.p.; the last target absorbs
    any rounding residual so the entry balances exactly.
    """
    _validate_targets(rule.targets)
    lines: list[dict[str, Any]] = []
    base_desc = description or f"Allocation: {rule.name}"

    # Credit the source pool
    lines.append({
        "account_id": str(rule.source_account_id),
        "debit": "0.00",
        "credit": str(round_money(amount)),
        "description": base_desc,
    })

    # Debit each target
    allocated = Decimal("0")
    for i, target in enumerate(rule.targets):
        pct = Decimal(str(target["percentage"])) / Decimal("100")
        label = target.get("label") or target.get("account_id", "")
        if i < len(rule.targets) - 1:
            share = round_money(amount * pct)
        else:
            # Last target absorbs rounding
            share = amount - allocated
        allocated += share
        lines.append({
            "account_id": str(target["account_id"]),
            "debit": str(share),
            "credit": "0.00",
            "description": f"{base_desc} — {label}",
        })

    return lines
