"""Bank-routing-identifier service — jurisdiction-neutral bank routing.

Generalises the legacy AU-only ``bsb`` / ``apca_user_id`` columns on
``Account`` / ``Contact`` / ``Employee`` / ``SuperFund`` to a table
keyed by ``(company_id, owner_type, owner_id, routing_scheme)`` where
``owner_type`` is one of ``KNOWN_OWNER_TYPES`` and ``routing_scheme``
is one of ``KNOWN_SCHEMES`` — extensible without an enum-altering
migration, the same posture ``services.business_identifiers`` uses.

The legacy columns are still authoritative for callers that read them
directly; this table is additive. Nothing here backfills the legacy
columns — unlike ``Company.abn``, bank details span four different
owner tables with no single migration-time source of truth, so
callers opt in per-owner by calling ``upsert``.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bank_routing_identifier import (
    BankRoutingIdentifier,
    BankRoutingOwnerType,
    BankRoutingScheme,
)

# Registry of accepted owner-table keys, derived from BankRoutingOwnerType
# (the single source of truth) so the two can never drift. The DB column
# stays free-text String(16) so a new entry is still a code-only change —
# adding a value to the enum here.
KNOWN_OWNER_TYPES: frozenset[str] = frozenset(t.value for t in BankRoutingOwnerType)

# Registry of accepted routing-scheme keys, derived from BankRoutingScheme
# (the single source of truth). The DB column stays free-text String(32)
# so a new scheme is still a code-only change.
KNOWN_SCHEMES: frozenset[str] = frozenset(s.value for s in BankRoutingScheme)


class UnknownOwnerType(ValueError):
    """Raised when a caller passes an owner_type outside ``KNOWN_OWNER_TYPES``."""


class UnknownScheme(ValueError):
    """Raised when a caller passes a routing_scheme outside ``KNOWN_SCHEMES``."""


def _validate_owner_type(owner_type: str) -> str:
    if owner_type not in KNOWN_OWNER_TYPES:
        raise UnknownOwnerType(
            f"Unknown bank-routing owner_type {owner_type!r}. "
            f"Accepted: {sorted(KNOWN_OWNER_TYPES)}"
        )
    return owner_type


def _validate_scheme(routing_scheme: str) -> str:
    if routing_scheme not in KNOWN_SCHEMES:
        raise UnknownScheme(
            f"Unknown bank-routing scheme {routing_scheme!r}. "
            f"Accepted: {sorted(KNOWN_SCHEMES)}"
        )
    return routing_scheme


async def get(
    session: AsyncSession,
    company_id: uuid.UUID,
    owner_type: str,
    owner_id: uuid.UUID,
    routing_scheme: str,
) -> BankRoutingIdentifier | None:
    """Return the routing row for ``(company_id, owner_type, owner_id,
    routing_scheme)`` or None."""
    _validate_owner_type(owner_type)
    _validate_scheme(routing_scheme)
    result = await session.execute(
        select(BankRoutingIdentifier).where(
            BankRoutingIdentifier.company_id == company_id,
            BankRoutingIdentifier.owner_type == owner_type,
            BankRoutingIdentifier.owner_id == owner_id,
            BankRoutingIdentifier.routing_scheme == routing_scheme,
        )
    )
    return result.scalars().first()


async def list_for_owner(
    session: AsyncSession,
    company_id: uuid.UUID,
    owner_type: str,
    owner_id: uuid.UUID,
) -> list[BankRoutingIdentifier]:
    """Return every routing scheme recorded for one owner row."""
    _validate_owner_type(owner_type)
    result = await session.execute(
        select(BankRoutingIdentifier).where(
            BankRoutingIdentifier.company_id == company_id,
            BankRoutingIdentifier.owner_type == owner_type,
            BankRoutingIdentifier.owner_id == owner_id,
        )
    )
    return list(result.scalars().all())


async def upsert(
    session: AsyncSession,
    company_id: uuid.UUID,
    owner_type: str,
    owner_id: uuid.UUID,
    routing_scheme: str,
    scheme_value: str,
    *,
    tenant_id: uuid.UUID | None = None,
    bic: str | None = None,
    account_number: str | None = None,
) -> BankRoutingIdentifier:
    """Insert or update the routing identifier for ``(company_id,
    owner_type, owner_id, routing_scheme)``.

    ``bic`` and ``account_number`` are optional and only overwrite the
    stored value when the caller actually supplies one (non-None) — a
    caller updating only ``scheme_value`` (e.g. fixing an IBAN typo)
    does not need to re-pass ``bic``/``account_number`` and will not
    silently wipe them. Mirrors the guard pattern in
    ``services.business_identifiers.upsert``.

    Caller is responsible for committing the session.
    """
    _validate_owner_type(owner_type)
    _validate_scheme(routing_scheme)
    existing = await get(session, company_id, owner_type, owner_id, routing_scheme)
    if existing is not None:
        existing.scheme_value = scheme_value
        if bic is not None:
            existing.bic = bic
        if account_number is not None:
            existing.account_number = account_number
        existing.updated_at = datetime.now(UTC)
        return existing

    row = BankRoutingIdentifier(
        company_id=company_id,
        owner_type=owner_type,
        owner_id=owner_id,
        routing_scheme=routing_scheme,
        scheme_value=scheme_value,
        bic=bic,
        account_number=account_number,
    )
    if tenant_id is not None:
        row.tenant_id = tenant_id
    session.add(row)
    await session.flush()
    return row
