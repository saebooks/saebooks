"""LT jurisdiction module — Lithuania's bolt-on compute package.

Sibling of ``saebooks.jurisdictions.au`` (the reference
implementation), ``nz`` and ``uk`` (the freshest precedents) under the
jurisdiction-module bolt-on architecture
(``~/records/saebooks/jurisdiction-module-architecture-design.md`` §2).
Strategically the third Baltic module beside EE — in code a fully
independent sibling (modules are independently failable; nothing here
imports EE compute).

* ``tax``         — ``LTTaxEngine`` (PVM compute incl. the
                    reverse-charge two-component fan-out for EU
                    acquisitions / Art 95 services / Art 96 domestic
                    RC) + ``fr0600_report`` (thin wrapper over the
                    data-driven return generator; box recipes in
                    ``seeds/jurisdictions/LT/
                    tax_return_box_definitions.yaml``). Reached via
                    the lazy ``_lt_factory`` in ``services.tax_engine``.
* ``payroll``     — ``LTPayrollEngine`` (progressive GPM with the
                    income-dependent NPD formula, employee Sodra
                    19.5% with the 60-VDU VSD ceiling, optional
                    II-pillar 3%, employer Sodra 1.77%/2.49%) with
                    hard refusals for the deliberately-unbuilt paths.
* ``identifiers`` — company-code / PVM-number / asmens-kodas check-
                    digit validators, registered into
                    ``services.business_identifiers`` at import (the
                    NZ ``nz_nzbn`` lazy-registration precedent).

Reference data ships in ``saebooks/seeds/jurisdictions/LT/`` (loader
tables + ``reference_seed: false`` module-data files — see each file's
header for its primary-pull provenance). The LT lodgement adapter stays
at ``services/lodgement/adapters/lt.py`` (in-file registry wiring, same
as AU/EE/NZ/UK) — offline, fail-loud ``LTLiveCredentialsMissing``
before any socket.

This ``__init__`` stays import-light (neutral payroll types only) so
``services.jurisdiction_modules`` can read the posting profile at
registration time without pulling the compute modules — those load
lazily on first engine dispatch (the AU Phase 0/1 discipline).

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

#: How LT's role-tagged payroll amounts land in the GL — account codes
#: from the LT chart_template seed (AVNT-class-shaped convenience
#: codes). Order is contractual (``PayrollPostingProfile``): after the
#: wages debit, expense legs post before liability credits:
#:
#:     Dr Wages and salaries (6301)              gross
#:     Dr Employer Sodra expense (6302)          employer social 1.77%/2.49%
#:        Cr Payroll taxes payable (4460)             GPM + employee Sodra
#:                                                     + II-pillar deduction
#:        Cr Employer Sodra payable (4463)            employer social
#:        Cr Net wages payable (4462)                 net
#:
#: GPM (to VMI) and the employee Sodra/II-pillar deductions (to Sodra)
#: both ride WITHHOLDING_LIABILITY into ONE payroll-taxes control
#: account (4460) — the UK precedent of one control account for
#: multiple rails, with the VMI/Sodra split happening at remittance
#: off the component-level audit notes. The II-pillar deduction is
#: employee-funded with NO employer component (the state pays the
#: incentive), so the RETIREMENT_* pair — whose finalize-path
#: reconstruction mirrors an EMPLOYER expense leg — is deliberately
#: NOT used for it; see ``payroll.py``'s role-mapping docstring.
PAYROLL_POSTING = PayrollPostingProfile(
    wages_account_code="6301",
    net_account_code="4462",
    wages_label="Wages",
    net_label="Net pay",
    role_accounts=(
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
            account_code="6302",
            label="Employer Sodra",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.WITHHOLDING_LIABILITY,
            account_code="4460",
            label="GPM/Sodra withheld",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
            account_code="4463",
            label="Employer Sodra payable",
        ),
    ),
)


def _lt_tax_factory() -> Any:
    # Moved verbatim from ``services.tax_engine._lt_factory`` (Job C).
    from saebooks.jurisdictions.lt.tax import LTTaxEngine

    return LTTaxEngine()


def _lt_payroll_factory() -> Any:
    # Moved verbatim from ``services.jurisdiction_modules._lt_payroll_factory``
    # (Job C).
    from saebooks.jurisdictions.lt.payroll import LTPayrollEngine

    return LTPayrollEngine()


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code="LT",
        label="Lithuania",
        provides_tax=True,          # registered below
        provides_payroll=True,      # registered below
        provides_lodgement=True,    # lodgement registry "LT", in place (live-gated)
        has_seed_dir=True,          # seeds/jurisdictions/LT/
    ),
    tax=_lt_tax_factory,
    payroll=_lt_payroll_factory,
    payroll_posting=PAYROLL_POSTING,
)

__all__ = ["PAYROLL_POSTING"]
