"""Chart-of-accounts (and reference-data) template dispatcher.

A template is a jurisdiction-specific bootstrap that loads the standard
CoA, tax codes, depreciation schedules, fiscal positions, and seed
period locks for a brand-new company. The registry maps a string key
to the function that knows how to apply it.

Layout
------
* ``apply_template(session, company_id, template_key)`` — public entry.
* ``_TEMPLATE_REGISTRY`` — maps key → applier coroutine. In-tree
  (jurisdiction-neutral or stub) appliers are listed statically; a
  jurisdiction package registers its own applier via
  ``register_template_applier`` on import (the Job C registration-
  inversion shape — see ``jurisdictions/ee``), so the core dispatcher
  never imports a jurisdiction module.

The AU applier delegates to ``saebooks.seed.load_au_coa`` so the
existing CSV-driven loader stays the source of truth for AU data.
NZ/UK appliers raise ``NotImplementedError`` until M1/M2. EE's applier
lives in ``jurisdictions/ee/chart.py`` and self-registers; the registry
readers call ``bootstrap.jurisdictions.ensure_loaded()`` so it is
present before a lookup concludes a key is unknown.
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


async def _apply_xx_default(session: AsyncSession, company: Company) -> None:
    """No-op applier for the neutral sentinel ("XX" — zero bolt-on
    jurisdiction modules, see ``services.jurisdiction_modules``).

    ``known_jurisdictions()`` advertises "XX" as a creatable jurisdiction,
    so company creation against it must actually succeed (jurisdiction-
    module Phase 0 design requirement: company creation works with zero
    jurisdiction modules). A neutral company legitimately gets no chart on
    creation — this exists only so "xx/default" is a registered,
    succeeding key instead of an ``UnknownTemplate`` trap for the one
    jurisdiction the schema layer otherwise dead-ends.
    """
    return None


_Applier = Callable[[AsyncSession, Company], Awaitable[None]]

_TEMPLATE_REGISTRY: dict[str, _Applier] = {
    "au/default": _apply_au_default,
    "nz/default": _apply_nz_default,
    "uk/default": _apply_uk_default,
    "xx/default": _apply_xx_default,
}

# The subset of ``_TEMPLATE_REGISTRY`` whose applier is genuinely
# IMPLEMENTED — not a ``NotImplementedError`` stub. ``known_jurisdictions``
# derives from this: a jurisdiction is creatable iff its default chart can
# actually be bootstrapped. NZ/UK have a live tax engine but only a stub
# CoA template (M1/M2); LT/LV have neither a template entry nor a CoA yet —
# both correctly excluded from onboarding. AU/XX are built in-tree; EE adds
# "ee/default" here via ``register_template_applier`` on package import.
_IMPLEMENTED_TEMPLATE_KEYS: set[str] = {
    "au/default",
    "xx/default",
}


def register_template_applier(
    template_key: str, applier: _Applier, *, implemented: bool = True
) -> None:
    """Register a jurisdiction package's chart-template applier.

    Called by ``saebooks.jurisdictions.<cc>`` packages at import time
    (via ``bootstrap.jurisdictions.ensure_loaded()``) so the neutral core
    never imports a jurisdiction module. ``implemented`` marks the key as
    a real (non-stub) applier, feeding ``known_jurisdictions()`` — the
    onboarding readiness signal. Re-registration overwrites (idempotent
    under repeated package import).
    """
    _TEMPLATE_REGISTRY[template_key] = applier
    if implemented:
        _IMPLEMENTED_TEMPLATE_KEYS.add(template_key)
    else:
        _IMPLEMENTED_TEMPLATE_KEYS.discard(template_key)


def _ensure_jurisdictions_loaded() -> None:
    """Import the enabled jurisdiction packages so their self-registered
    template appliers are present. Function-scoped (never at module load)
    so the core→bootstrap→jurisdictions edge stays lazy — no import cycle
    and no jurisdiction import in the neutral core's import graph."""
    from saebooks.bootstrap.jurisdictions import ensure_loaded

    ensure_loaded()


async def apply_template(
    session: AsyncSession,
    company_id: uuid.UUID,
    template_key: str,
) -> None:
    """Apply the named template to ``company_id``.

    The applier runs in the caller's transaction; the caller is
    responsible for commit. Raises ``UnknownTemplate`` for unknown keys
    and ``NotImplementedError`` for stub jurisdictions (NZ/UK).
    """
    applier = _TEMPLATE_REGISTRY.get(template_key)
    if applier is None:
        # Lazy guard (Job C shape): jurisdiction packages register their
        # applier on import, so ensure the enabled set is loaded before
        # concluding the key is unknown (e.g. "ee/default").
        _ensure_jurisdictions_loaded()
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
    _ensure_jurisdictions_loaded()
    return sorted(_TEMPLATE_REGISTRY)


def known_jurisdictions() -> list[str]:
    """Return the jurisdiction codes a company can be CREATED against
    (sorted) — the request-validation source of truth for
    ``CompanyCreate.jurisdiction``.

    A jurisdiction is creatable iff it has an *implemented* (non-stub)
    default chart template (see ``_IMPLEMENTED_TEMPLATE_KEYS``): AU, EE and
    the neutral sentinel XX today. This is deliberately narrower than the
    set of jurisdictions with a registered tax engine
    (``tax_engine._REGISTRY`` — NZ/UK/LT/LV all have live engines but no
    built CoA), because onboarding a company with no chart of accounts is
    not a working state. The donor branch derived the same {AU, EE, XX}
    set from tax-engine stubs; on this head the tax engines are live, so
    template-implementation is the correct readiness signal.
    """
    _ensure_jurisdictions_loaded()
    return sorted({key.split("/", 1)[0].upper() for key in _IMPLEMENTED_TEMPLATE_KEYS})
