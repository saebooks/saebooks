"""Chart-of-accounts (and reference-data) template dispatcher.

A template is a jurisdiction-specific bootstrap that loads the standard
CoA, tax codes, depreciation schedules, fiscal positions, and seed
period locks for a brand-new company. The registry maps a string key
to the function that knows how to apply it.

Layout
------
* ``apply_template(session, company_id, template_key)`` — public entry.
* ``_TEMPLATE_REGISTRY`` — maps key → applier coroutine. Adding a new
  jurisdiction is a code-only change here plus a new applier.

The AU applier delegates to ``saebooks.seed.load_au_coa`` so the
existing CSV-driven loader stays the source of truth for AU data.
NZ/UK/EE appliers raise ``NotImplementedError`` until M1/M2/M3.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.company import Company


class UnknownTemplate(KeyError):
    """Raised when a caller asks for a template key not in the registry."""


async def _apply_au_default(session: AsyncSession, company: Company) -> None:
    """Run the full AU bootstrap for the given company.

    Wraps the existing ``saebooks.seed.load_au_coa`` helpers — keeping
    the CSV+_EXTRA_ACCOUNTS+raw-shadow logic in one place rather than
    duplicating it here. This is the canonical AU path until the seed
    module is fully retired.
    """
    # Local imports to break the import cycle between
    # services.templates and seed.load_au_coa (the seed module imports
    # from services for tax-code/companies bootstraps).
    from saebooks.seed.load_au_coa import (
        _load_accounts,
        _load_depreciation_models,
        _load_raw,
        _seed_period_locks,
    )
    from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes
    from saebooks.services.tax_codes import (
        ensure_international_seed as ensure_intl_tax_codes,
    )

    await ensure_tax_codes(session, company.id)
    await ensure_intl_tax_codes(session, company.id)
    await _load_accounts(session, company)
    await _seed_period_locks(session, company)

    raw_sources = [
        ("raw_au_tax_codes", "account.tax-au.csv"),
        ("raw_au_tax_groups", "account.tax.group-au.csv"),
        ("raw_au_fiscal_positions", "account.fiscal.position-au.csv"),
        ("raw_au_account_tags", "account.account.tag.csv"),
        ("raw_au_depreciation_models", "account.depreciation.model-au.csv"),
    ]
    for table, name in raw_sources:
        await _load_raw(session, table, name)

    await _load_depreciation_models(session)


async def _apply_nz_default(session: AsyncSession, company: Company) -> None:
    raise NotImplementedError("Template stub — implemented in M1")


async def _apply_uk_default(session: AsyncSession, company: Company) -> None:
    raise NotImplementedError("Template stub — implemented in M2")


async def _apply_ee_default(session: AsyncSession, company: Company) -> None:
    raise NotImplementedError("Template stub — implemented in M3")


_Applier = Callable[[AsyncSession, Company], Awaitable[None]]

_TEMPLATE_REGISTRY: dict[str, _Applier] = {
    "au/default": _apply_au_default,
    "nz/default": _apply_nz_default,
    "uk/default": _apply_uk_default,
    "ee/default": _apply_ee_default,
}


async def apply_template(
    session: AsyncSession,
    company_id: uuid.UUID,
    template_key: str,
) -> None:
    """Apply the named template to ``company_id``.

    The applier runs in the caller's transaction; the caller is
    responsible for commit. Raises ``UnknownTemplate`` for unknown keys
    and ``NotImplementedError`` for stub jurisdictions (NZ/UK/EE).
    """
    applier = _TEMPLATE_REGISTRY.get(template_key)
    if applier is None:
        raise UnknownTemplate(
            f"Unknown CoA template {template_key!r}. "
            f"Known: {sorted(_TEMPLATE_REGISTRY)}"
        )

    company = await session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")

    await applier(session, company)


def known_templates() -> list[str]:
    """Return the list of registered template keys (sorted)."""
    return sorted(_TEMPLATE_REGISTRY)
