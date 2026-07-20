"""Payroll-engine dispatcher — per-jurisdiction strategy modules.

Jurisdiction-module architecture Phase 0: the payroll seam, mirroring
``tax_engine.get_engine`` (tax) and ``lodgement.registry.get_adapter``
(lodgement) — the third per-capability registry keyed on
``Company.jurisdiction`` (never a ``FLAG_*``; edition gating is the
orthogonal ``require_feature`` axis).

Public surface
--------------

* ``PayrollEngine`` — runtime-checkable Protocol every per-jurisdiction
  implementation satisfies. Async, and the engine owns its own
  reference reads (see ``types.py`` docstring for why this differs from
  the sync/no-I/O ``TaxEngine``).
* ``get_payroll_engine(jurisdiction)`` — registry dispatcher. Never
  raises for an unknown jurisdiction: unregistered codes (including
  the reserved neutral sentinel ``"XX"``) degrade to
  ``NeutralPayrollEngine`` — the null-object floor that makes "zero
  modules keeps books" and "capabilities independently failable" real
  (a jurisdiction can ship tax without payroll and the payroll leg
  degrades to neutral without breaking tax).
* ``register_engine(jurisdiction, factory)`` — programmatic
  registration hook used by
  ``services.jurisdiction_modules.register_jurisdiction_module``.
* ``get_posting_profile(jurisdiction)`` /
  ``register_posting_profile(...)`` — the Phase 1 posting seam: which
  account code each ``PayrollComponentRole`` books to when the core
  posts the pay-run JE (``pay_runs_v2.finalize_with_je``).
  Unregistered codes degrade to ``NEUTRAL_POSTING_PROFILE`` (wages +
  net only), same null-object contract as the engine registry.

Registration model
------------------

This registry starts EMPTY. Each jurisdiction package
(``saebooks.jurisdictions.<cc>``) self-registers its payroll engine at
import time via ``services.jurisdiction_modules.
register_jurisdiction_module`` (Job C registration inversion —
``jurisdiction_modules`` itself no longer imports any jurisdiction
package). ``get_payroll_engine``/``get_posting_profile`` lazily call
``saebooks.bootstrap.jurisdictions.ensure_loaded()`` on first dispatch
so every config-selected jurisdiction's registration is always in
effect without requiring app-startup ordering, while avoiding a
module-level import cycle (jurisdiction packages import this package's
``register_engine`` at module level, so the dependency must point one
way — this package never imports a jurisdiction package or the
bootstrap at module level, only inside these functions).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from saebooks.services.payroll.neutral import (
    NEUTRAL_JURISDICTION,
    NEUTRAL_POSTING_PROFILE,
    NeutralPayrollEngine,
)
from saebooks.services.payroll.types import (
    PayrollComponent,
    PayrollComponentRole,
    PayrollContext,
    PayrollPostingProfile,
    PayrollResult,
    PayrollRoleAccount,
)


@runtime_checkable
class PayrollEngine(Protocol):
    """Per-jurisdiction payroll compute interface.

    Consumes a neutral ``PayrollContext``, returns a neutral
    ``PayrollResult`` — amounts + account-role tags, never accounts,
    never a posted JE (the core owns the double-entry). Async because
    payroll compute genuinely reads reference tables (AU:
    ``payg_tax_scales``/``stsl_coefficients``; EE: the reference DB
    with embedded fallback) — each engine owns its own reads and its
    own reference-DB-absent degradation.
    """

    jurisdiction: str

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        """Compute withholding/contribution components for one line.

        Deterministic for a given ``ctx`` + reference-table state; the
        result is snapshotted onto the pay-run line.
        """
        ...


_REGISTRY: dict[str, Any] = {}

# Posting profiles (jurisdiction-module Phase 1): how each registered
# jurisdiction's role-tagged amounts land in the GL — see
# ``types.PayrollPostingProfile``. Same registration model as
# ``_REGISTRY``: starts empty, populated by ``jurisdiction_modules``,
# unregistered codes degrade to ``NEUTRAL_POSTING_PROFILE``.
_POSTING_PROFILES: dict[str, PayrollPostingProfile] = {}

# One-shot guard for the lazy jurisdiction_modules import in
# get_payroll_engine — see "Registration model" in the module docstring.
_modules_loaded = False


def register_engine(jurisdiction: str, factory: Any) -> None:
    """Register (or replace) the payroll-engine factory for a jurisdiction."""
    _REGISTRY[jurisdiction] = factory


def register_posting_profile(
    jurisdiction: str, profile: PayrollPostingProfile
) -> None:
    """Register (or replace) a jurisdiction's payroll posting profile."""
    _POSTING_PROFILES[jurisdiction] = profile


def _ensure_modules_registered() -> None:
    global _modules_loaded
    if not _modules_loaded:
        # Job C registration inversion: jurisdiction_modules no longer
        # self-populates by importing every country module — the
        # app-level bootstrap does, config-driven. Local import to keep
        # this package import-light and avoid a module-level cycle
        # (jurisdiction packages import register_engine from this
        # package at module level, so the dependency must point one
        # way).
        from saebooks.bootstrap.jurisdictions import ensure_loaded

        ensure_loaded()
        _modules_loaded = True


def get_payroll_engine(jurisdiction: str) -> PayrollEngine:
    """Return the payroll engine for a jurisdiction.

    Registered code → that module's engine. Anything else — the
    reserved neutral sentinel ``"XX"`` or a jurisdiction with no
    payroll module bolted on — degrades to ``NeutralPayrollEngine``
    (null object: net = gross - deductions, zero statutory components).
    """
    _ensure_modules_registered()
    factory = _REGISTRY.get(jurisdiction)
    if factory is None:
        return NeutralPayrollEngine()
    return factory()


def get_posting_profile(jurisdiction: str) -> PayrollPostingProfile:
    """Return the payroll posting profile for a jurisdiction.

    Same degrade contract as ``get_payroll_engine``: a registered code
    gets its module's profile; anything else (the ``"XX"`` sentinel, a
    jurisdiction with no payroll module) gets
    ``NEUTRAL_POSTING_PROFILE`` — wages + net only, zero statutory
    roles. Never raises.
    """
    _ensure_modules_registered()
    return _POSTING_PROFILES.get(jurisdiction, NEUTRAL_POSTING_PROFILE)


__all__ = [
    "NEUTRAL_JURISDICTION",
    "NEUTRAL_POSTING_PROFILE",
    "NeutralPayrollEngine",
    "PayrollComponent",
    "PayrollComponentRole",
    "PayrollContext",
    "PayrollEngine",
    "PayrollPostingProfile",
    "PayrollResult",
    "PayrollRoleAccount",
    "get_payroll_engine",
    "get_posting_profile",
    "register_engine",
    "register_posting_profile",
]
