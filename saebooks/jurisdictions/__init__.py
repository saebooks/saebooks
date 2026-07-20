"""Jurisdiction-module packages — the bolt-on country modules.

Jurisdiction-module architecture Phase 1 (design:
``~/records/saebooks/jurisdiction-module-architecture-design.md``).
Each subpackage is one country's **bolt-on module**: the
jurisdiction-specific compute (and, in later phases, lodgement builders
and integrations) that the neutral core dispatches to through the
per-capability registries (``services.tax_engine.get_engine``,
``services.payroll.get_payroll_engine``,
``services.lodgement.registry.get_adapter``), keyed on
``Company.jurisdiction``.

Dependency direction: modules import the core (neutral types, models,
money helpers) — the core never imports a module at module level.
The two sanctioned crossings are lazy/registration-time only:
``services.jurisdiction_modules`` (the registration entry point) and
lazy per-call imports documented at their sites.

Phase 1 ships ``au`` (super/PAYG compute + the AU payroll engine +
AU super-fund vehicle validation). EE is still dispatched from its
in-place ``services.payroll_ee``/``services.fringe_benefits_ee``
registrations — formalising it as ``jurisdictions/ee`` is Phase 5.
"""
