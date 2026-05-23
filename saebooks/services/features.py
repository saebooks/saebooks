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

import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from saebooks.config import Settings
from saebooks.config import settings as _default_settings

_log = logging.getLogger(__name__)

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

# --- B/46 (AI document extraction via Claude Haiku vision) --------------- #
# Requires a live Anthropic API call per document — costs real money, so
# Community (free) and Offline (perpetual / no-phone-home) tiers never get
# it. Business+ only.
FLAG_AI_EXTRACTION = "ai_extraction"

# --- B/49 (overhead allocation rules — multi-company overhead split) ----- #
# Useful for any entity with multiple companies / cost centres sharing
# overhead. Gated at Business+ since multi-company is the primary driver.
FLAG_ALLOCATION_RULES = "allocation_rules"

# --- Developer-only flags (not part of any published / billable tier) ----- #
# These exist ONLY in the ``developer`` tier — Richard's personal instances
# (sauer / gecairns / app-preview / cashbook-demo) where the codebase is
# also the dev surface. They MUST NOT appear in any commercial tier — see
# memory feedback_sauer-instance-no-guardrails for the rationale.
#
# FLAG_HARD_DELETE — admin can hard-delete rows directly from the ledger
#   (skips the soft-archive / reverse-JE workflow that public editions
#   enforce for ATO retention compliance).
FLAG_HARD_DELETE = "hard_delete"

# FLAG_DEV_TOOLS — exposes developer affordances in the UI: edit-anything
#   override on frozen-state entities (APPROVED time entries, POSTED
#   journals, etc.), raw-JSON entity inspectors, internal endpoints.
FLAG_DEV_TOOLS = "dev_tools"

ALL_FLAGS: tuple[str, ...] = (
    FLAG_HARD_DELETE,
    FLAG_DEV_TOOLS,
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
})

# Developer tier — internal-only. Superset of enterprise + every dev-only
# flag. NOT a billable subscription; never offered through Stripe checkout;
# only activated via SAEBOOKS_EDITION=developer in the .env of an instance
# the owner controls directly. Used by Richard for his personal books
# (sauer / gecairns / app-preview / cashbook-demo) so test rows can be
# hard-deleted, frozen-state entities can be edited, etc., without the
# ATO retention guardrails that ship to paying customers.
_DEVELOPER_FLAGS: frozenset[str] = _ENTERPRISE_FLAGS | frozenset({
    FLAG_HARD_DELETE,
    FLAG_DEV_TOOLS,
})

_TIER_FLAGS: dict[str, frozenset[str]] = {
    "community": frozenset(),
    "offline": _OFFLINE_FLAGS,
    "business": _BUSINESS_FLAGS,
    "pro": _PRO_FLAGS,
    "enterprise": _ENTERPRISE_FLAGS,
    "developer": _DEVELOPER_FLAGS,
}

# Tier display order — used by /admin/license to render the edition
# comparison matrix left-to-right, cheapest to dearest. "developer" is
# internal-only and intentionally last; the licence resolver should hide
# it from the public comparison matrix.
TIER_ORDER: tuple[str, ...] = (
    "community",
    "offline",
    "business",
    "pro",
    "enterprise",
    "developer",
)


# ---------------------------------------------------------------------- #
# Public API (stable — callers in routers/ and services/ depend on it)   #
# ---------------------------------------------------------------------- #


def is_enabled(
    flag: str,
    *,
    settings: Settings | None = None,
    edition: str | None = None,
) -> bool:
    """Return ``True`` when ``flag`` is active under the given settings.

    ``settings`` defaults to the module-level singleton; pass an explicit
    ``Settings`` for tests that want to exercise alternate editions
    without monkey-patching.

    ``edition`` is an explicit override that bypasses ``settings`` —
    used by ``require_feature`` to apply a per-user effective edition
    (e.g. launch-promo Pro JWT) without mutating the singleton. When
    both are passed, ``edition`` wins.

    Unknown flags raise ``ValueError`` — typoed flag names should fail
    loud rather than silently return ``False`` (which would hide a
    paid-tier feature in a paid-tier build).
    """
    if flag not in _ALL_FLAGS_SET:
        raise ValueError(f"Unknown feature flag: {flag!r}")
    if edition is not None:
        return flag in _TIER_FLAGS.get(edition, frozenset())
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


def _effective_edition_for_request(request: Request | None) -> str:
    """Return the edition that gates feature access for this request.

    The launch-promo flow stamps a Pro-tier JWT on
    ``users.launch_promo_jwt`` for the first 1,000 customers. Without
    a per-request override, those users would still bind to
    ``settings.edition`` (Community for the SaaS deployment) and
    silently lose every Pro feature they were promised.

    Resolution order:

    1. ``request.state.user.launch_promo_jwt`` (verified, non-expired)
       via ``resolve_licence_for_user`` — wins over the singleton so
       a promo'd user sees Pro on a Community-default deployment.
    2. ``_default_settings.edition`` — the process-wide singleton,
       used for unauthenticated routes, system jobs, and CLI calls.

    Test failures resolving the per-user JWT (bad sig, expired, no
    portal pubkey) fall through to the singleton — never to a *lower*
    tier than the user would otherwise have. Defensive: a corrupt
    promo JWT must not deny baseline access.
    """
    if request is None:
        return _default_settings.edition
    user = getattr(request.state, "user", None)
    if user is None:
        return _default_settings.edition

    # Lazy import — features.py is imported during settings module load
    # in some paths and the resolver pulls in the full licence package.
    from saebooks.services.licence.resolver import resolve_licence_for_user

    try:
        licence = resolve_licence_for_user(user)
    except Exception:  # defensive — never fail-closed on resolver glitch
        _log.exception(
            "feature gate: resolve_licence_for_user raised; "
            "falling back to settings.edition"
        )
        return _default_settings.edition
    return licence.edition


def require_feature(flag: str) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency factory: 404 when ``flag`` is disabled.

    Attach via ``Depends(require_feature(FLAG_X))`` or on a router via
    ``dependencies=[Depends(require_feature(FLAG_X))]``.

    The dep is **per-request** as of the launch-promo fix: the gate
    looks up ``request.state.user`` (stamped by ``require_bearer``)
    and consults ``resolve_licence_for_user`` so a user with a Pro
    promo JWT gets Pro features even when ``settings.edition`` is
    Community. Routes without an authenticated user fall back to the
    singleton edition.

    Returns 404 (not 403) so a lower-tier build doesn't advertise the
    existence of higher-tier routes — they simply aren't part of the
    build, which matches how the feature looks from the outside.
    """
    if flag not in _ALL_FLAGS_SET:
        raise ValueError(f"Unknown feature flag: {flag!r}")

    async def _dep(request: Request) -> None:
        edition = _effective_edition_for_request(request)
        if not is_enabled(flag, edition=edition):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return _dep
