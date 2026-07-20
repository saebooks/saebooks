"""NZ jurisdiction module — New Zealand's bolt-on compute package.

Built fresh against the jurisdiction-module contract (design doc §2;
AU is the Phase 1/2 reference implementation, EE the non-AU proof):

* ``tax``         — ``NZTaxEngine`` (15% GST determination) +
                    ``gst101_report`` (GST101A via the data-driven
                    ``tax_return_generator`` off the NZ box seed).
                    Reached via the lazy ``_nz_factory`` in
                    ``services.tax_engine`` (AU's in-file wiring shape).
* ``payroll``     — ``NZPayrollEngine`` (PAYE code-based withholding
                    incl. ACC earner's levy, KiwiSaver employee +
                    employer, ESCT, student loan) with EMBEDDED dated
                    rate tables (the ``super_calc`` convention;
                    reference-preferred re-sourcing is a later phase).
                    Registered via ``services.jurisdiction_modules``.
* ``identifiers`` — IRD-number / NZBN / bank-account validators. The
                    NZBN GS1 check-digit validator registers itself into
                    ``services.business_identifiers`` at import (the
                    ``nz_ird`` mod-11 double-pass validator already
                    lives in core — verified, not duplicated here).

Reference data lives at ``saebooks/seeds/jurisdictions/NZ/`` (loaded by
``services.reference.loader``). The NZ lodgement adapter lives at
``jurisdictions/nz/lodgement.py`` and is self-registered below via
``services.lodgement.registry.register_lodgement_adapter`` (the first
adapter moved out of ``services/lodgement/adapters/``; AU/UK/EE/LT/LV
follow in a later phase).

This ``__init__`` stays import-light (neutral payroll types + the two
core registration seams ONLY — no ``saebooks.db``, no runtime env) so
``services.jurisdiction_modules`` can read the posting profile at
registration time without pulling the compute modules — those load
lazily on first engine dispatch, same as AU. The lodgement adapter is
likewise registered as a lazy factory: ``lodgement.py`` imports only on
first ``get_adapter("NZ")``. ``identifiers`` is NOT
imported here: ``services.business_identifiers`` transitively imports
``saebooks.db`` (which demands runtime env config), so pulling it at
registration time would break the import-light contract. Instead
``tax.py`` and ``payroll.py`` import it, so the ``nz_nzbn`` validator
activates on first NZ compute dispatch; until then
``validate("nz_nzbn", ...)`` stays no-opinion (``None``) — exactly the
pre-module core behaviour, a clean degrade.

Job C registration inversion: this package SELF-registers its tax +
payroll engines at import time (moved in from the core registration
hubs — see ``jurisdictions/au/__init__.py`` for the reference shape).
"""
from __future__ import annotations

from typing import Any

from saebooks.services.jurisdiction_modules import (
    JurisdictionModuleDescriptor,
    register_jurisdiction_module,
)
from saebooks.services.lodgement.registry import register_lodgement_adapter
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollPostingProfile,
    PayrollRoleAccount,
)

#: How NZ's role-tagged payroll amounts land in the GL. Account codes
#: follow the AU profile's hyphenated convention (``6-2110`` etc. — the
#: pay-run fixtures' chart shape) rather than the 4-digit chart_template
#: codes, mirroring the exact AU precedent (see ``jurisdictions/au/
#: __init__.py``). Per-employee JE shape:
#:
#:     Dr Wages expense (6-2110)                 gross
#:     Dr KiwiSaver employer expense (6-2120)    ks_employer (incl. ESCT)
#:        Cr PAYE & deductions payable (2-1310)      paye + student loan
#:                                                    + KiwiSaver employee
#:        Cr KiwiSaver payable (2-1320)               ks_employer (incl. ESCT)
#:        Cr Net clearing (2-1150)                    net
#:
#: PAYE (incl. the ACC earner's levy), the student-loan deduction and
#: the employee KiwiSaver deduction are all carved out of gross AND all
#: remitted to Inland Revenue together through payday filing, so one
#: WITHHOLDING_LIABILITY account is the accounting-correct NZ shape.
#: ESCT rides inside the RETIREMENT pair (the employer contribution is
#: booked gross; the ESCT-to-IR vs net-to-fund split happens at
#: remittance) — see ``payroll.py``'s module docstring for why the
#: current role enum + pay-run line schema make that the only
#: non-lossy mapping.
PAYROLL_POSTING = PayrollPostingProfile(
    wages_account_code="6-2110",
    net_account_code="2-1150",
    role_accounts=(
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_EXPENSE,
            account_code="6-2120",
            label="KiwiSaver ER",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.WITHHOLDING_LIABILITY,
            account_code="2-1310",
            label="PAYE & deductions",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_LIABILITY,
            account_code="2-1320",
            label="KiwiSaver payable",
        ),
    ),
)


def _nz_tax_factory() -> Any:
    # Moved verbatim from ``services.tax_engine._nz_factory`` (Job C).
    from saebooks.jurisdictions.nz.tax import NZTaxEngine

    return NZTaxEngine()


def _nz_payroll_factory() -> Any:
    # Moved verbatim from ``services.jurisdiction_modules._nz_payroll_factory``
    # (Job C).
    from saebooks.jurisdictions.nz.payroll import NZPayrollEngine

    return NZPayrollEngine()


def _nz_lodgement_factory() -> Any:
    # Lazy so importing this package never pulls the adapter module;
    # ``get_adapter("NZ")`` resolves it on first use (Job C shape).
    from saebooks.jurisdictions.nz.lodgement import NZLodgementAdapter

    return NZLodgementAdapter()


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code="NZ",
        label="New Zealand",
        provides_tax=True,          # registered below
        provides_payroll=True,      # registered below
        provides_lodgement=True,    # registered below (shaped, live-gated)
        has_seed_dir=True,          # seeds/jurisdictions/NZ/
    ),
    tax=_nz_tax_factory,
    payroll=_nz_payroll_factory,
    payroll_posting=PAYROLL_POSTING,
)

register_lodgement_adapter("NZ", _nz_lodgement_factory)

__all__ = ["PAYROLL_POSTING"]
