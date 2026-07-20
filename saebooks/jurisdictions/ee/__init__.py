"""EE (Estonia) jurisdiction module surface.

EE tax/payroll/lodgement engines are still dispatched by their in-place
registrations under ``saebooks.services`` (formalising EE as a registered
``jurisdiction_modules`` bundle is Phase 5 — see
``services/jurisdiction_modules.py``). This package is the landing spot
for EE-specific logic:

* ``validators.py`` — company-registry field format validators
  (``registrikood`` / ``kmv_number``), the raising form the API schema
  layer calls to 422 a malformed value, kept off core schema code.
* ``identifiers.py`` — the same shapes as non-raising
  ``business_identifiers`` scheme validators (``ee_regcode`` / ``ee_vat``
  ``check_digit_valid``), registered lazily on first EE dispatch exactly
  like ``jurisdictions/lv/identifiers.py``.
* ``chart.py`` — the ``ee/default`` chart-of-accounts template applier.

This package SELF-REGISTERS on import (``bootstrap.jurisdictions`` lists
``"ee"`` in ``_MODULE_PATHS``, so ``ensure_loaded()`` imports it): the
``ee/default`` template applier and the EE control-account convention
codes. That is how the neutral core dispatches to EE without importing a
jurisdiction module — the Job C registration-inversion shape, the same
as ``jurisdictions/nz`` self-registering its lodgement adapter.

It MUST stay import-light — register only a *lazy* applier factory and
the literal control codes, never import ``chart.py`` or ``identifiers.py``
at module load. ``chart.py`` pulls ``saebooks.db``/``ReferenceSession``
(runtime env), so importing it eagerly would break the contract that
``ensure_loaded()`` can run at boot; the factory pulls it only on first
``ee/default`` apply. Likewise ``identifiers`` stays activated on first
chart apply (``chart.py`` imports it), so ``ee_regcode``/``ee_vat`` keep
their lazy "first EE dispatch" registration.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# EE convention control-account codes — the AR/AP leaves of the EE chart.
# Kept here (import-light literals) rather than in ``chart.py`` so both the
# control-account resolver (``services.control_accounts``) and the template
# applier (``chart.py``) share one source without core importing the heavy
# chart module. Distinct from the AU convention codes ("1-1200"/"2-1200")
# in ``services.control_accounts``, which do not exist in an EE chart.
EE_AR_CONTROL_CODE = "1200"
EE_AP_CONTROL_CODE = "2100"

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from saebooks.models.company import Company


async def _apply_ee_default(session: AsyncSession, company: Company) -> None:
    """``ee/default`` template applier — lazy factory.

    Imports ``chart.py`` only here (on first apply) so this package stays
    import-light at registration time. Registered with
    ``services.templates.register_template_applier`` below.
    """
    from saebooks.jurisdictions.ee.chart import apply_ee_chart_template

    await apply_ee_chart_template(session, company)


def _register() -> None:
    """Register EE's core-dispatched seams. Runs at package import; called
    (idempotently) by ``bootstrap.jurisdictions.ensure_loaded()``."""
    from saebooks.services.control_accounts import (
        register_control_account_defaults,
    )
    from saebooks.services.templates import register_template_applier

    register_template_applier("ee/default", _apply_ee_default, implemented=True)
    register_control_account_defaults(
        "EE", ar_code=EE_AR_CONTROL_CODE, ap_code=EE_AP_CONTROL_CODE
    )
    # EE cashbook (EUR, käibemaks-aware) — lifts the v1 AU-only cashbook
    # restriction for Estonian companies. Category data lives in
    # ``cashbook.py`` (this package); pure dataclasses, import-light.
    from saebooks.jurisdictions.ee.cashbook import EE_CASHBOOK_PROFILE
    from saebooks.services.cashbook_categories import register_cashbook_profile

    register_cashbook_profile(EE_CASHBOOK_PROFILE)


_register()
