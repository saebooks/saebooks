"""Feature-flag / licence-gate module.

Per ``CHARTER.md §6`` (v1.1 — five-edition model) the SAE Books
codebase is a single AGPL tree that supports five editions arranged
as a strict superset:

* **Community** (free, AGPL) — complete single-company bookkeeping,
  no paid-API integrations, stock theme only. No flags.
* **Offline** (once-off USB-bound licence) — Community +
  multi-currency, inventory, projects/budgets, v2 asset register,
  granular permissions, themes, SMTP relay, extended audit modes.
* **Business** (subscription) — Offline + multi-company (cap 2 per
  licence), AU bank feeds, ABR lookup, Stripe + Paperless
  integrations.
* **Pro** (subscription) — Business + international lookups (LEI,
  Companies House), ATO SBR e-lodgement, QBO import, SQL tool,
  scheduled backups, audit snapshots.
* **Enterprise** (subscription + setup fee) — Pro + per-company
  SISS credentials. Support SLA is contractual, not a flag.

All features live in this repo, but UI routes that surface
tier-gated features must be *runtime-gated* via ``require_feature``
so a lower edition never silently ships a feature gated above it.
Routes return 404 (not 403) so a lower-tier install doesn't
advertise the existence of paid-tier endpoints — they simply aren't
part of the build from the outside.

The source of truth for the active edition is ``settings.edition``
(configured via ``SAEBOOKS_EDITION``). The licence resolver in
``services/licence/`` sets this at boot from the USB Ed25519 licence
(Offline) or portal JWT (Business/Pro/Enterprise), falling back to
``community`` when nothing is present.

Superset invariant
------------------
Every tier must contain every flag of every tier below it. This
encodes the CHARTER §6.2 upgradeability guarantee: a customer who
pays to move up can never lose a feature they already had. Enforced
by ``test_tier_superset_invariant`` in ``tests/test_features.py``.

Usage::

    from fastapi import APIRouter, Depends
    from saebooks.services.features import (
        FLAG_BANK_FEEDS, require_feature,
    )

    router = APIRouter(prefix="/admin/bank-feeds")

    @router.get("/", dependencies=[Depends(require_feature(FLAG_BANK_FEEDS))])
    async def index() -> ...: ...
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, status

from saebooks.config import Settings
from saebooks.config import settings as _default_settings

# ---------------------------------------------------------------------- #
# Flag identifiers                                                       #
# ---------------------------------------------------------------------- #

# --- v1.0 flags (pre-five-edition, preserved verbatim) --------------- #
FLAG_BANK_FEEDS = "bank_feeds"
FLAG_ABR_LOOKUP = "abr_lookup"
FLAG_LEI_LOOKUP = "lei_lookup"
FLAG_COMPANIES_HOUSE = "companies_house"
FLAG_MULTI_COMPANY = "multi_company"
FLAG_EXTENDED_AUDIT_MODES = "extended_audit_modes"
FLAG_PER_COMPANY_SISS = "per_company_siss"
FLAG_ATO_SBR = "ato_sbr"

# --- v1.1 flags (added with the five-edition rollout) ---------------- #
FLAG_MULTI_CURRENCY = "multi_currency"
FLAG_INVENTORY = "inventory"
FLAG_PROJECTS_BUDGETS = "projects_budgets"
FLAG_ASSET_V2 = "asset_v2"
FLAG_GRANULAR_PERMISSIONS = "granular_permissions"
FLAG_THEMES = "themes"
FLAG_SMTP_RELAY = "smtp_relay"
FLAG_STRIPE_INTEGRATION = "stripe_integration"
FLAG_PAPERLESS_INTEGRATION = "paperless_integration"
FLAG_QBO_IMPORT = "qbo_import"
FLAG_SQL_TOOL = "sql_tool"
FLAG_AUDIT_SNAPSHOTS = "audit_snapshots"
FLAG_SCHEDULED_BACKUPS = "scheduled_backups"

# --- AI document extraction --------------------------------------------- #
# Vision-capable LLM extraction of receipts/invoices via an
# OpenAI-compatible API. Each call costs real money, so Community (free)
# and Offline (perpetual / no-phone-home) tiers never get it.
# Business+ only.
FLAG_AI_EXTRACTION = "ai_extraction"

# --- B/49 (overhead allocation rules — multi-company overhead split) ----- #
# Useful for any entity with multiple companies / cost centres sharing
# overhead. Gated at Business+ since multi-company is the primary driver.
FLAG_ALLOCATION_RULES = "allocation_rules"

# --- Build #9 (accounting-package sync — Enterprise tier) ---------------- #
# Bidirectional sync with external accounting packages (Xero / MYOB / QBO).
# The umbrella flag gates the whole feature surface (Settings -> Sync UI,
# /api/v1/sync/* routes, the worker entry-point). The three sub-flags each
# gate a specific provider so a customer can enable e.g. Xero alone while
# their MYOB connection waits on the accountant's call.
#
# Decided 2026-05-06 as Enterprise-only — bidirectional sync is "extra
# work" beyond Pro's banking + lodgement scope. See plan
# `~/.claude/plans/saebooks-accounting-sync.md` § "Tier" for the
# decision log.
FLAG_ACCOUNTING_SYNC = "accounting_sync"
FLAG_SYNC_XERO = "sync_xero"
FLAG_SYNC_MYOB = "sync_myob"
FLAG_SYNC_QBO = "sync_qbo"

ALL_FLAGS: tuple[str, ...] = (
    FLAG_BANK_FEEDS,
    FLAG_ABR_LOOKUP,
    FLAG_LEI_LOOKUP,
    FLAG_COMPANIES_HOUSE,
    FLAG_MULTI_COMPANY,
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_PER_COMPANY_SISS,
    FLAG_ATO_SBR,
    FLAG_MULTI_CURRENCY,
    FLAG_INVENTORY,
    FLAG_PROJECTS_BUDGETS,
    FLAG_ASSET_V2,
    FLAG_GRANULAR_PERMISSIONS,
    FLAG_THEMES,
    FLAG_SMTP_RELAY,
    FLAG_STRIPE_INTEGRATION,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_QBO_IMPORT,
    FLAG_SQL_TOOL,
    FLAG_AUDIT_SNAPSHOTS,
    FLAG_SCHEDULED_BACKUPS,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
    FLAG_ACCOUNTING_SYNC,
    FLAG_SYNC_XERO,
    FLAG_SYNC_MYOB,
    FLAG_SYNC_QBO,
)

_ALL_FLAGS_SET: frozenset[str] = frozenset(ALL_FLAGS)


# ---------------------------------------------------------------------- #
# Tier → flag mapping (strict superset — CHARTER §6.2)                   #
# ---------------------------------------------------------------------- #
# Offline sits above Community by adding every non-network productivity
# feature. It doesn't get bank feeds / ABR / Stripe / Paperless because
# those are live-API integrations and Offline is perpetual / no-phone-
# home — we can't guarantee upstream availability for a keystroke-era
# sale. Multi-company stays Business+ because Offline is capped at 1
# company by the licence model (CHARTER §7.1).

_OFFLINE_FLAGS: frozenset[str] = frozenset({
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_MULTI_CURRENCY,
    FLAG_INVENTORY,
    FLAG_PROJECTS_BUDGETS,
    FLAG_ASSET_V2,
    FLAG_GRANULAR_PERMISSIONS,
    FLAG_THEMES,
    FLAG_SMTP_RELAY,
})

_BUSINESS_FLAGS: frozenset[str] = _OFFLINE_FLAGS | frozenset({
    FLAG_MULTI_COMPANY,
    FLAG_BANK_FEEDS,
    FLAG_ABR_LOOKUP,
    FLAG_STRIPE_INTEGRATION,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
})

_PRO_FLAGS: frozenset[str] = _BUSINESS_FLAGS | frozenset({
    FLAG_LEI_LOOKUP,
    FLAG_COMPANIES_HOUSE,
    FLAG_ATO_SBR,
    FLAG_QBO_IMPORT,
    FLAG_SQL_TOOL,
    FLAG_AUDIT_SNAPSHOTS,
    FLAG_SCHEDULED_BACKUPS,
})

_ENTERPRISE_FLAGS: frozenset[str] = _PRO_FLAGS | frozenset({
    FLAG_PER_COMPANY_SISS,
    # Build #9 — bidirectional accounting-package sync. Umbrella + per-
    # provider sub-flags. All four are Enterprise-only; flipping the
    # umbrella off via the licence JWT disables every sub-flag too
    # because the sync router checks the umbrella before the sub-flag.
    FLAG_ACCOUNTING_SYNC,
    FLAG_SYNC_XERO,
    FLAG_SYNC_MYOB,
    FLAG_SYNC_QBO,
})

_TIER_FLAGS: dict[str, frozenset[str]] = {
    "community": frozenset(),
    "offline": _OFFLINE_FLAGS,
    "business": _BUSINESS_FLAGS,
    "pro": _PRO_FLAGS,
    "enterprise": _ENTERPRISE_FLAGS,
}

# Tier display order — used by /admin/license to render the edition
# comparison matrix left-to-right, cheapest to dearest.
TIER_ORDER: tuple[str, ...] = (
    "community",
    "offline",
    "business",
    "pro",
    "enterprise",
)


# ---------------------------------------------------------------------- #
# Public API (stable — callers in routers/ and services/ depend on it)   #
# ---------------------------------------------------------------------- #


def is_enabled(flag: str, *, settings: Settings | None = None) -> bool:
    """Return ``True`` when ``flag`` is active under the given settings.

    ``settings`` defaults to the module-level singleton; pass an explicit
    ``Settings`` for tests that want to exercise alternate editions
    without monkey-patching.

    Unknown flags raise ``ValueError`` — typoed flag names should fail
    loud rather than silently return ``False`` (which would hide a
    paid-tier feature in a paid-tier build).
    """
    if flag not in _ALL_FLAGS_SET:
        raise ValueError(f"Unknown feature flag: {flag!r}")
    effective = settings if settings is not None else _default_settings
    return flag in _TIER_FLAGS.get(effective.edition, frozenset())


def active_flags(*, settings: Settings | None = None) -> dict[str, bool]:
    """Return ``{flag_name: enabled}`` for every known flag.

    Used by ``/admin/license`` to render the flag matrix.
    """
    return {flag: is_enabled(flag, settings=settings) for flag in ALL_FLAGS}


def tier_flags(tier: str) -> frozenset[str]:
    """Return the frozenset of flags enabled at ``tier``.

    Raises ``ValueError`` for an unknown tier. Handy for the
    ``/admin/license`` matrix renderer and for tests.
    """
    if tier not in _TIER_FLAGS:
        raise ValueError(f"Unknown edition: {tier!r}")
    return _TIER_FLAGS[tier]


def require_feature(flag: str) -> Callable[[], Awaitable[None]]:
    """FastAPI dependency factory: 404 when ``flag`` is disabled.

    Attach via ``Depends(require_feature(FLAG_X))`` or on a router via
    ``dependencies=[Depends(require_feature(FLAG_X))]``.

    Returns 404 (not 403) so a lower-tier build doesn't advertise the
    existence of higher-tier routes — they simply aren't part of the
    build, which matches how the feature looks from the outside.
    """
    if flag not in _ALL_FLAGS_SET:
        raise ValueError(f"Unknown feature flag: {flag!r}")

    async def _dep() -> None:
        if not is_enabled(flag):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return _dep
