"""AU jurisdiction module — Australia's bolt-on compute package.

Jurisdiction-module architecture Phase 1: the AU-specific payroll/super
compute physically lives here, beside the engine that calls it —

* ``payg``        — PAYG withholding (ATO Schedule 1 / NAT 1004 formulas
                    against the ``payg_tax_scales``/``stsl_coefficients``
                    reference tables). Moved from ``services/payg.py``.
* ``super_calc``  — Super Guarantee from OTE (SGAA 1992), with the SG
                    rate history deliberately EMBEDDED (design doc §5.4:
                    re-sourcing to the ``super_guarantee_rates``
                    reference table is a separate later phase). Moved
                    from ``services/super_calc.py``.
* ``payroll``     — ``AUPayrollEngine``, the ``PayrollEngine``
                    implementation registered for ``"AU"``. Moved from
                    ``services/payroll/au.py``.
* ``super_funds`` — AU superannuation-vehicle validation rules
                    (APRA USI / SMSF ABN+ESA), extracted from the
                    jurisdiction-neutral CRUD in
                    ``services/super_funds.py``.

Phase 2 moved the AU tax compute in beside it —

* ``tax``             — ``AUTaxEngine`` (GST compute + auto-post +
                        BAS report/settlement + GST account-config
                        validation). Moved from
                        ``services/tax_engine/au.py``; the lazy
                        ``_au_factory`` in ``services.tax_engine``
                        now imports it from here.
* ``tpar``            — Taxable Payments Annual Report build/finalise
                        /CSV. Moved from ``services/tpar.py``.
* ``dutiable_events`` — stamp/transfer duty events (reads the shared
                        ``duty_rate_schedules``/``duty_surcharge_rates``
                        reference tables — reference data stays in the
                        shared DB by decision). Moved from
                        ``services/dutiable_events.py``.
* ``bas`` / ``gst``   — the M0 deprecated re-export shims over ``tax``,
                        moved with it. Dropped at M1 entry.

Phase 3 moved the AU integration packages in —

* ``abr``     — Australian Business Register lookup (contact enrichment
                from an ABN via ``abr.business.gov.au``). Moved from
                ``services/abr/``.
* ``ato_sbr`` — ATO SBR Machine-Credential keystore / onboarding wizard
                / EVTE ping (commercial — excluded from the public
                export). Moved from ``services/ato_sbr/``.

This ``__init__`` stays import-light (neutral payroll types only) so
``services.jurisdiction_modules`` can read the posting profile at
registration time without pulling the compute modules — those load
lazily on first engine dispatch, same as Phase 0.

AU lodgement moves in a later phase; its registration is unchanged by
this package.

Job C registration inversion (neutral-core strip): this package now
SELF-registers its tax + payroll engines at import time via
``register_jurisdiction_module`` (moved in from the core registration
hubs — was ``tax_engine._au_factory`` /
``jurisdiction_modules._au_payroll_factory``). Importing
``saebooks.services.jurisdiction_modules`` here is the one sanctioned
module→core edge (the core itself never imports this package at
module level any more — see
``saebooks.bootstrap.jurisdictions.ensure_loaded``, which is what
actually triggers this import).
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

#: How AU's role-tagged payroll amounts land in the GL (the account
#: codes the AU CoA seed provides — previously ``pay_runs_v2``'s
#: hardcoded ``_ACCT_*`` constants and its AU-only JE branch). Order is
#: contractual (see ``PayrollPostingProfile``): after the wages debit,
#: legs post Dr SG expense / Cr PAYG WH / Cr Super payable — the exact
#: pre-Phase-1 per-employee JE shape:
#:
#:     Dr Wages expense (6-2110)  gross
#:     Dr Super expense (6-2120)  sg
#:        Cr PAYG WH     (2-1310)       payg
#:        Cr Super payable (2-1320)     sg
#:        Cr Net clearing (2-1150)      net
PAYROLL_POSTING = PayrollPostingProfile(
    wages_account_code="6-2110",
    net_account_code="2-1150",
    role_accounts=(
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_EXPENSE,
            account_code="6-2120",
            label="SG",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.WITHHOLDING_LIABILITY,
            account_code="2-1310",
            label="PAYG WH",
        ),
        PayrollRoleAccount(
            role=PayrollComponentRole.RETIREMENT_LIABILITY,
            account_code="2-1320",
            label="Super payable",
        ),
    ),
)


def _au_tax_factory() -> Any:
    # Local import to avoid pulling AU compute until first dispatch —
    # moved verbatim from ``services.tax_engine._au_factory`` (Job C).
    from saebooks.jurisdictions.au.tax import AUTaxEngine

    return AUTaxEngine()


def _au_payroll_factory() -> Any:
    # Moved verbatim from ``services.jurisdiction_modules._au_payroll_factory``
    # (Job C) — local import so registering AU doesn't pull payg/super_calc
    # until first dispatch.
    from saebooks.jurisdictions.au.payroll import AUPayrollEngine

    return AUPayrollEngine()


def _au_validate_retirement_account(**fields: Any) -> None:
    # Neutral-core strip Job D: local import so registering AU doesn't
    # pull ``super_funds`` (and its ``services.super_funds.SuperFundError``
    # import) until first validation call — same lazy-shim idiom as
    # ``_au_tax_factory``/``_au_payroll_factory`` above.
    from saebooks.jurisdictions.au.super_funds import validate_fund_fields

    validate_fund_fields(**fields)


register_jurisdiction_module(
    JurisdictionModuleDescriptor(
        code="AU",
        label="Australia",
        provides_tax=True,          # registered below
        provides_payroll=True,      # registered below
        provides_lodgement=True,    # lodgement registry "AU", in place
        has_seed_dir=True,          # seeds/jurisdictions/AU/
        min_edition_for_lodgement="pro",  # FLAG_ATO_SBR gates at the router
    ),
    tax=_au_tax_factory,
    payroll=_au_payroll_factory,
    payroll_posting=PAYROLL_POSTING,
    retirement_validator=_au_validate_retirement_account,
)

__all__ = ["PAYROLL_POSTING"]
