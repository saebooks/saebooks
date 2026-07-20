"""Single AR/AP control-account resolver — Packet 4b.

Every posting site that touches the receivables / payables control
account (invoices, bills, payments, credit notes, supplier credit
notes, bad-debt write-offs, FX revaluation, the cashbook edition
backfill) used to hardcode the AU chart-of-accounts convention codes
``"1-1200"`` (Trade Debtors) / ``"2-1200"`` (Trade Creditors) — seven
separate module-level ``_AR_CODE`` / ``_AP_CODE`` constants. A company
whose chart uses a different code (e.g. an EE company) had no way to
tell the engine; the EE demo build had to re-code its chart to match
AU convention instead of the other way round.

This module is the ONE place that resolves the code, reading
``Company.ar_control_account_code`` / ``Company.ap_control_account_code``
(0198) and falling back to a per-jurisdiction default when unset — so
every existing (AU) company resolves to exactly the code it always did.
Every call site above should route through here instead of hardcoding
its own constant.

Non-AU onboarding: the fallback used to be unconditionally the AU codes
(``AR_DEFAULT_CODE``/``AP_DEFAULT_CODE``), which do not exist in a chart
that uses a different convention (an EE chart uses ``1200``/``2100``). A
PATCH that clears such a company's override back to NULL then resolved to
a dead AU code at posting time. The fallback is now chosen from
``Company.jurisdiction`` via a registry: AU is the built-in default (so
the resolver never depends on any jurisdiction module being loaded), and
a jurisdiction package registers its own convention codes on import via
``register_control_account_defaults`` (the Job C registration-inversion
shape — the core names no country).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.company import Company

# The AU chart-of-accounts convention codes — unchanged default for
# every AU company that hasn't set an override. AU resolves via these
# built-ins, NOT via ``_CONTROL_DEFAULTS``, so the AU fallback is
# independent of whether any jurisdiction module has loaded.
AR_DEFAULT_CODE = "1-1200"
AP_DEFAULT_CODE = "2-1200"

# jurisdiction code -> (ar_code, ap_code). Populated by jurisdiction
# packages on import via ``register_control_account_defaults`` (e.g.
# ``jurisdictions/ee`` registers ("EE", "1200", "2100")). AU is
# deliberately absent — it uses the built-in constants above.
_CONTROL_DEFAULTS: dict[str, tuple[str, str]] = {}


def register_control_account_defaults(
    jurisdiction: str, *, ar_code: str, ap_code: str
) -> None:
    """Register a jurisdiction's AR/AP control-account convention codes.

    Called by ``saebooks.jurisdictions.<cc>`` packages at import time so
    the core resolver never names or imports a jurisdiction module.
    Re-registration overwrites (idempotent under repeated package import).
    """
    _CONTROL_DEFAULTS[jurisdiction] = (ar_code, ap_code)


def _control_defaults(jurisdiction: str | None) -> tuple[str, str] | None:
    """Return ``(ar, ap)`` convention codes for a non-AU jurisdiction, or
    ``None`` to fall back to the AU built-ins. Lazily loads the enabled
    jurisdiction packages (Job C shape) on a registry miss so a packaged
    jurisdiction's codes are present before we conclude there are none."""
    if not jurisdiction or jurisdiction == "AU":
        return None
    codes = _CONTROL_DEFAULTS.get(jurisdiction)
    if codes is None:
        from saebooks.bootstrap.jurisdictions import ensure_loaded

        ensure_loaded()
        codes = _CONTROL_DEFAULTS.get(jurisdiction)
    return codes


def default_ar_code(jurisdiction: str | None) -> str:
    """The AR control-account fallback code for a company's jurisdiction."""
    codes = _control_defaults(jurisdiction)
    return codes[0] if codes else AR_DEFAULT_CODE


def default_ap_code(jurisdiction: str | None) -> str:
    """The AP control-account fallback code for a company's jurisdiction."""
    codes = _control_defaults(jurisdiction)
    return codes[1] if codes else AP_DEFAULT_CODE


async def resolve_ar_code(session: AsyncSession, company_id: uuid.UUID) -> str:
    """The Trade Debtors (AR) control-account code for this company.

    NULL/blank ``Company.ar_control_account_code`` falls back to
    ``default_ar_code(company.jurisdiction)`` — unchanged AU behaviour for
    AU companies, the EE convention code for EE companies.
    """
    row = (
        await session.execute(
            select(Company.ar_control_account_code, Company.jurisdiction).where(
                Company.id == company_id
            )
        )
    ).one_or_none()
    if row is None:
        return AR_DEFAULT_CODE
    code, jurisdiction = row
    code = (code or "").strip()
    return code or default_ar_code(jurisdiction)


async def resolve_ap_code(session: AsyncSession, company_id: uuid.UUID) -> str:
    """The Trade Creditors (AP) control-account code for this company.

    NULL/blank ``Company.ap_control_account_code`` falls back to
    ``default_ap_code(company.jurisdiction)`` — unchanged AU behaviour for
    AU companies, the EE convention code for EE companies.
    """
    row = (
        await session.execute(
            select(Company.ap_control_account_code, Company.jurisdiction).where(
                Company.id == company_id
            )
        )
    ).one_or_none()
    if row is None:
        return AP_DEFAULT_CODE
    code, jurisdiction = row
    code = (code or "").strip()
    return code or default_ap_code(jurisdiction)


async def _get_account_or_raise(
    session: AsyncSession,
    company_id: uuid.UUID,
    code: str,
    *,
    label: str,
    setting_hint: str,
    error_cls: Callable[[str], Exception],
) -> Account:
    acct = (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == code,
                # Fixer round 5 (F2): exclude archived accounts — mirrors
                # ``pay_runs_v2._account_by_company_column``'s
                # ``archived_at.is_(None)`` filter (added in the same
                # diff). Without it, a company that archives its control
                # account during a chart cleanup without setting an
                # override would silently keep resolving to the dead
                # archived row instead of raising this loud config error.
                Account.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise error_cls(
            f"{label} control account {code!r} is missing — re-run the CoA "
            f"seed, or check companies.{setting_hint}."
        )
    return acct


async def get_ar_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    error_cls: Callable[[str], Exception] = ValueError,
) -> Account:
    """Resolve this company's AR (Trade Debtors) control account.

    Raises ``error_cls(message)`` — a loud config error, never a
    silently-unbalanced posting — when the resolved code does not
    exist in the company's chart. Callers pass their own domain
    exception type (e.g. ``InvoiceError``) so existing ``except``
    clauses keep working unchanged.
    """
    code = await resolve_ar_code(session, company_id)
    return await _get_account_or_raise(
        session,
        company_id,
        code,
        label="AR",
        setting_hint="ar_control_account_code",
        error_cls=error_cls,
    )


async def get_ap_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    error_cls: Callable[[str], Exception] = ValueError,
) -> Account:
    """Resolve this company's AP (Trade Creditors) control account.

    See ``get_ar_account`` — same loud-error contract.
    """
    code = await resolve_ap_code(session, company_id)
    return await _get_account_or_raise(
        session,
        company_id,
        code,
        label="AP",
        setting_hint="ap_control_account_code",
        error_cls=error_cls,
    )
