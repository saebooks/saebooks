"""UK jurisdiction module — the United Kingdom's bolt-on compute package.

Sibling of ``saebooks.jurisdictions.au`` (the reference implementation)
under the jurisdiction-module bolt-on architecture
(``~/records/saebooks/jurisdiction-module-architecture-design.md`` §2):

* ``tax``         — ``UKTaxEngine`` (VAT compute incl. the reverse-
                    charge/PVA two-component fan-out) + ``vat100_report``
                    (thin wrapper over the data-driven return generator;
                    box recipes in ``seeds/jurisdictions/UK/
                    tax_return_box_definitions.yaml``).
* ``payroll``     — ``UKPayrollEngine`` (cumulative three-nation PAYE,
                    Class 1 NI, student loans, auto-enrolment) with
                    hard refusals for the deliberately-unbuilt paths
                    (K codes, week 53, BIK payrolling, ...).
* ``identifiers`` — VAT number dual mod-97/mod-9755 validation, CRN/
                    NINO/PAYE-ref/Accounts-Office-ref format checks,
                    UTR format-only (check digit parked).

Reference data ships in ``saebooks/seeds/jurisdictions/UK/`` (loader
tables + ``reference_seed: false`` module-data files — see each file's
header for its primary-pull provenance).

UK lodgement stays in ``services/lodgement/adapters/uk.py`` (registered
in-file in the lodgement registry, like AU's) — offline, fail-loud
``UKLiveCredentialsMissing`` before any socket; transport/OAuth/fraud-
prevention headers are a later phase.

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

#: How the UK's role-tagged payroll amounts land in the GL — account
#: codes from the UK chart_template seed (Sage-convention nominal
#: codes). Order is contractual (``PayrollPostingProfile``): after the
#: wages debit, expense legs post before liability credits:
#:
#:     Dr Gross wages (7000)                 gross
#:     Dr Employer's NI (7006)               employer NI
#:     Dr Employer's pensions (7007)         employer pension
#:        Cr PAYE/NI/SL payable (2210)             PAYE + employee NI + student loans
#:        Cr PAYE/NI/SL payable (2210)             employer NI
#:        Cr Pension payable (2230)                employee + employer pension
#:        Cr Net wages control (2220)              net
#:
#: WITHHOLDING_LIABILITY and EMPLOYER_SOCIAL_LIABILITY both map to 2210
#: deliberately: PAYE, employee NI, student loans AND employer NI are
#: all remitted to HMRC in the single monthly PAYE payment, so one
#: HMRC-payroll-liabilities control account is the UK-conventional
#: shape (two JE lines, same account).
PAYROLL_POSTING = PayrollPostingProfile(
    wages_account_code="7000",
    net_account_code="2220",
    wages_label="Gross wages",
    net_label="Net pay",
    role_accounts=(
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
            account_code="7006",
            label="Employer NI",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_EXPENSE,
            account_code="7007",
            label="Employer pension",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.WITHHOLDING_LIABILITY,
            account_code="2210",
            label="PAYE/NI/SL",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
            account_code="2210",
            label="Employer NI payable",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_LIABILITY,
            account_code="2230",
            label="Pension payable",
        ),
    ),
)


def _uk_tax_factory() -> Any:
    # Moved verbatim from ``services.tax_engine._uk_factory`` (Job C).
    from saebooks.jurisdictions.uk.tax import UKTaxEngine

    return UKTaxEngine()


def _uk_payroll_factory() -> Any:
    # Moved verbatim from ``services.jurisdiction_modules._uk_payroll_factory``
    # (Job C).
    from saebooks.jurisdictions.uk.payroll import UKPayrollEngine

    return UKPayrollEngine()


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code="UK",
        label="United Kingdom",
        provides_tax=True,          # registered below
        provides_payroll=True,      # registered below
        provides_lodgement=True,    # lodgement registry "UK", in place (live-gated)
        has_seed_dir=True,          # seeds/jurisdictions/UK/
    ),
    tax=_uk_tax_factory,
    payroll=_uk_payroll_factory,
    payroll_posting=PAYROLL_POSTING,
)

__all__ = ["PAYROLL_POSTING"]
