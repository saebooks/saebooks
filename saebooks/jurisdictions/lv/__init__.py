"""LV jurisdiction module — Latvia's bolt-on compute package.

Sibling of ``saebooks.jurisdictions.au`` (the reference implementation),
``nz`` and ``uk``, built fresh against the jurisdiction-module contract
(``~/records/saebooks/jurisdiction-module-architecture-design.md`` §2).
Latvia is the second Baltic module: strategically a satellite of the
EE/Tasur beachhead, but in code a fully independent sibling — nothing
here imports EE compute (modules are independently failable; the only
cross-module import is the jurisdiction-neutral GL account-type sets
from ``jurisdictions.au.tax``, the same reuse ``ee.py``/``nz``/``uk``
make).

* ``tax``         — ``LVTaxEngine`` (PVN determination incl. the
                    reverse-charge two-component fan-out for EU
                    acquisitions and third-country services, routed to
                    Latvia's dedicated declaration rows — NOT the EE
                    fold-into-the-domestic-box shape) + ``pvn_report``
                    (thin wrapper over the data-driven return
                    generator; row recipes in ``seeds/jurisdictions/LV/
                    tax_return_box_definitions.yaml``) +
                    ``compute_uin_on_distribution`` (the
                    distributed-profits corporate-tax arithmetic:
                    standard 20% on base ÷0.8, and the 2026 elective
                    15% on base ÷0.85 + 6% IIN).
* ``payroll``     — ``LVPayrollEngine`` (flat-25.5% monthly IIN after
                    the fixed non-taxable minimum + dependant
                    allowances, VSAOI employer/employee) with hard
                    refusals for the deliberately-unbuilt paths.
* ``identifiers`` — reģistrācijas numurs / PVN number mod-11
                    validators (registered into
                    ``services.business_identifiers`` on first LV
                    compute dispatch — the NZ lazy-registration
                    discipline) + personas kods checksum (a personal
                    identifier: plain module function, not a
                    business-identifier scheme).

Reference data ships in ``saebooks/seeds/jurisdictions/LV/`` (loader
tables + ``reference_seed: false`` module-data files). The LV lodgement
adapter stays at ``services/lodgement/adapters/lv.py`` (in-file registry
wiring, same as AU/EE/NZ/UK) — offline, fail-loud
``LVLiveCredentialsMissing`` before any socket; the EDS transport is a
later phase (credentials parked).

This ``__init__`` stays import-light (neutral payroll types only) so
``services.jurisdiction_modules`` can read the posting profile at
registration time without pulling the compute modules — those load
lazily on first engine dispatch (the AU Phase 0/1 discipline).
``identifiers`` is NOT imported here for the same reason the NZ module
documents: ``services.business_identifiers`` transitively imports
``saebooks.db``; ``tax.py``/``payroll.py`` import it instead, so the
``lv_pvn``/``lv_regnum`` validators activate on first LV dispatch.

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
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollPostingProfile,
    PayrollRoleAccount,
)

#: How LV's role-tagged payroll amounts land in the GL — account codes
#: from the LV chart_template seed. Per-employee JE shape:
#:
#:     Dr Darba algas (6100)                       gross
#:     Dr VSAOI izmaksas — darba devēja daļa (6110)  employer VSAOI
#:        Cr Ieturētie nodokļi VID (2300)               IIN + employee VSAOI
#:        Cr VSAOI saistības — darba devēja daļa (2310) employer VSAOI
#:        Cr Darba algas saistības (2400)               net
#:
#: WITHHOLDING_LIABILITY carries BOTH the IIN and the employee VSAOI
#: deduction: since 2021 every Latvian payroll tax is remitted into the
#: single vienotais nodokļu konts (unified tax account) at VID, so one
#: withheld-taxes control account is the accounting-correct LV shape
#: (the UK module's PAYE/NI single-account rationale, verbatim). There
#: are deliberately NO retirement roles: Latvia's 2nd pension pillar is
#: a STATE-side redirect inside VSAOI (see mandatory_contribution_rules
#: .yaml) — neither employer nor employee pays a separate contribution,
#: so a pay run has no retirement leg at all (the EE "no super" proof
#: that capabilities are independently present, replayed for LV).
PAYROLL_POSTING = PayrollPostingProfile(
    wages_account_code="6100",
    net_account_code="2400",
    wages_label="Darba alga (gross)",
    net_label="Neto alga",
    role_accounts=(
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
            account_code="6110",
            label="VSAOI darba devēja daļa",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.WITHHOLDING_LIABILITY,
            account_code="2300",
            label="IIN + VSAOI ieturējumi",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
            account_code="2310",
            label="VSAOI darba devēja daļa — saistības",
        ),
    ),
)


def _lv_tax_factory() -> Any:
    # Moved verbatim from ``services.tax_engine._lv_factory`` (Job C).
    from saebooks.jurisdictions.lv.tax import LVTaxEngine

    return LVTaxEngine()


def _lv_payroll_factory() -> Any:
    # Moved verbatim from ``services.jurisdiction_modules._lv_payroll_factory``
    # (Job C).
    from saebooks.jurisdictions.lv.payroll import LVPayrollEngine

    return LVPayrollEngine()


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code="LV",
        label="Latvia",
        provides_tax=True,          # registered below
        provides_payroll=True,      # registered below
        provides_lodgement=True,    # lodgement registry "LV", in place (shaped, live-gated)
        has_seed_dir=True,          # seeds/jurisdictions/LV/
    ),
    tax=_lv_tax_factory,
    payroll=_lv_payroll_factory,
    payroll_posting=PAYROLL_POSTING,
)

__all__ = ["PAYROLL_POSTING"]
