"""Business-identifier service — per-jurisdiction company IDs.

Generalises the legacy ``Company.abn`` column to a child table keyed
by ``(company_id, scheme)`` where ``scheme`` is one of the values
in ``KNOWN_SCHEMES`` (extensible — adding a new jurisdiction means
appending here, not altering an enum).

The legacy ``Company.abn`` column is still authoritative for callers
that read it directly; on write the M0 path is to use ``upsert`` here
which mirrors to the child table. The migration backfills existing
``companies.abn`` rows so reads through this service are immediately
consistent on existing installs.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.business_identifier import BusinessIdentifier

# Registry of accepted scheme keys. Extensible — keep DB column as
# free-text String(32) so a new entry here is a code-only change.
KNOWN_SCHEMES: frozenset[str] = frozenset({
    "au_abn",
    "au_acn",
    "nz_nzbn",
    "uk_crn",
    "ee_regcode",
    "global_lei",
})


class UnknownScheme(ValueError):
    """Raised when a caller passes a scheme outside ``KNOWN_SCHEMES``."""


def _validate_scheme(scheme: str) -> str:
    if scheme not in KNOWN_SCHEMES:
        raise UnknownScheme(
            f"Unknown business-identifier scheme {scheme!r}. "
            f"Accepted: {sorted(KNOWN_SCHEMES)}"
        )
    return scheme


async def get(
    session: AsyncSession,
    company_id: uuid.UUID,
    scheme: str,
) -> BusinessIdentifier | None:
    """Return the identifier row for ``(company_id, scheme)`` or None."""
    _validate_scheme(scheme)
    result = await session.execute(
        select(BusinessIdentifier).where(
            BusinessIdentifier.company_id == company_id,
            BusinessIdentifier.scheme == scheme,
        )
    )
    return result.scalars().first()


async def upsert(
    session: AsyncSession,
    company_id: uuid.UUID,
    scheme: str,
    value: str,
    *,
    tenant_id: uuid.UUID | None = None,
    verified_at: datetime | None = None,
) -> BusinessIdentifier:
    """Insert or update the identifier for ``(company_id, scheme)``.

    Caller is responsible for committing the session.
    """
    _validate_scheme(scheme)
    existing = await get(session, company_id, scheme)
    if existing is not None:
        existing.value = value
        existing.updated_at = datetime.now(UTC)
        if verified_at is not None:
            existing.verified_at = verified_at
        return existing

    row = BusinessIdentifier(
        company_id=company_id,
        scheme=scheme,
        value=value,
        verified_at=verified_at,
    )
    if tenant_id is not None:
        row.tenant_id = tenant_id
    session.add(row)
    await session.flush()
    return row
