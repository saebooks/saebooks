"""Jurisdiction-module registration — the bolt-on entry point.

Design: ``~/records/saebooks/jurisdiction-module-architecture-design.md``
(§2). A jurisdiction module is a **registration bundle** that populates
the existing per-capability registries —

* tax:       ``services.tax_engine.register_engine``
* payroll:   ``services.payroll.register_engine``
* lodgement: ``services.lodgement.registry`` (``_ADAPTERS``)

— plus a thin :class:`JurisdictionModuleDescriptor` for introspection
(mirrors ``module_registry.ModuleEntry``: a static catalogue, never
per-request state). Dispatch stays per-capability, keyed on
``Company.jurisdiction``, so capabilities are independently present
and independently failable — a capability a module does NOT register
falls through to that registry's neutral/null fallback.

Two-axis reminder (settled decision 4): jurisdiction selection is
``Company.jurisdiction``; edition gating is ``require_feature``. A
module may CONTAIN edition-gated capabilities (AU lodgement is Pro —
``min_edition_for_lodgement``), but there is never a ``FLAG_<country>``.

Job C registration inversion (neutral-core strip): this module used to
import AU/NZ/UK/LT/LV at module level and call
``register_jurisdiction_module`` for each — a core→module import, the
very thing the neutral-core strip removes. Now this module registers
only the **XX** neutral sentinel (descriptor-only, provides nothing —
its null-object engines are core-owned, pre-wired in their registries
at ``tax_engine/neutral.py`` / ``payroll/neutral.py``). Every real
jurisdiction (AU/NZ/UK/LT/LV) calls ``register_jurisdiction_module``
ITSELF, from its own ``saebooks.jurisdictions.<cc>.__init__`` at import
time. What imports those packages in the first place is
``saebooks.bootstrap.jurisdictions.ensure_loaded()`` (an app-level
concern, config-driven, deliberately OUTSIDE ``services``/``models``) —
``get_descriptor``/``list_descriptors`` below call it lazily so the
catalogue is always populated by the time anything reads it, with no
core→module import anywhere in this file.

EE is still dispatched by its in-place registrations (tax_engine/
lodgement registries + the ``pay_runs_v2`` EE branch) — it is
core-native (no ``jurisdictions/ee`` package) and has no descriptor
here, unchanged from before Job C.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from saebooks.services import payroll as payroll_registry
from saebooks.services import tax_engine as tax_registry


@dataclass(frozen=True, slots=True)
class JurisdictionModuleDescriptor:
    """What does this jurisdiction module provide? (introspection only)

    A static catalogue row, exactly like ``module_registry.ModuleEntry``
    keeps per-request state out: per-company resolution ("is AU
    lodgement entitled for THIS tenant at its edition?") stays in the
    usage endpoint / ``require_feature``, never here.
    """

    code: str                  # ISO — "AU", "EE"; "XX" = neutral sentinel
    label: str                 # "Australia"
    provides_tax: bool
    provides_payroll: bool
    provides_lodgement: bool
    has_seed_dir: bool
    min_edition_for_lodgement: str | None = None  # edition-axis note only


_DESCRIPTORS: dict[str, JurisdictionModuleDescriptor] = {}


def register_jurisdiction_module(
    descriptor: JurisdictionModuleDescriptor,
    *,
    tax: Any | None = None,
    payroll: Any | None = None,
    payroll_posting: Any | None = None,
    lodgement: Any | None = None,
    retirement_validator: Any | None = None,
) -> None:
    """Bolt a jurisdiction module onto the engine.

    Writes each supplied factory into its per-capability registry and
    records the descriptor. Every capability argument is optional — a
    module registers only what it provides (EE has no super; a tax-only
    launch registers tax alone); the rest degrade to that registry's
    neutral/null fallback. A factory already wired in-file in its
    registry (AU tax, AU lodgement today) is simply not re-passed here;
    the descriptor still records that the jurisdiction provides it.

    ``retirement_validator`` (neutral-core strip Job D): a callable
    accepting the retirement-account vehicle-law fields
    (``is_smsf``/``usi``/``employer_abn``/``esa`` today — AU's shape;
    a non-AU module would supply its own vehicle-law callable with
    whatever fields it needs) and raising on violation. Registered
    into ``services.super_funds``'s own tiny per-jurisdiction registry
    — same neutral-degrade contract as ``payroll_posting`` above: a
    jurisdiction with no vehicle-law module bolted on validates
    nothing (a generic retirement account is still creatable).
    """
    _DESCRIPTORS[descriptor.code] = descriptor
    if tax is not None:
        tax_registry.register_engine(descriptor.code, tax)
    if payroll is not None:
        payroll_registry.register_engine(descriptor.code, payroll)
    if payroll_posting is not None:
        payroll_registry.register_posting_profile(
            descriptor.code, payroll_posting
        )
    if lodgement is not None:
        from saebooks.services.lodgement import registry as lodgement_registry

        lodgement_registry._ADAPTERS[descriptor.code] = lodgement
    if retirement_validator is not None:
        from saebooks.services import super_funds as super_funds_registry

        super_funds_registry.register_retirement_validator(
            descriptor.code, retirement_validator
        )


def get_descriptor(code: str) -> JurisdictionModuleDescriptor | None:
    from saebooks.bootstrap.jurisdictions import ensure_loaded

    ensure_loaded()
    return _DESCRIPTORS.get(code)


def list_descriptors() -> list[JurisdictionModuleDescriptor]:
    from saebooks.bootstrap.jurisdictions import ensure_loaded

    ensure_loaded()
    return [_DESCRIPTORS[code] for code in sorted(_DESCRIPTORS)]


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code=tax_registry.NEUTRAL_JURISDICTION,
        label="No jurisdiction (neutral)",
        provides_tax=False,
        provides_payroll=False,
        provides_lodgement=False,
        has_seed_dir=False,
    ),
)


__all__ = [
    "JurisdictionModuleDescriptor",
    "get_descriptor",
    "list_descriptors",
    "register_jurisdiction_module",
]
