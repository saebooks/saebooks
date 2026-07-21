"""The module registry — single source of truth for ``GET /api/v1/modules``
and ``GET /api/v1/modules/usage`` (M2 §5 build-sequence steps 3-6).

Per ``m2-module-architecture-audit-2026-07-09.md`` §3, this is a static
catalogue (id / label / kind / group / tier_membership / state / static
wrapped-flag metadata for delegated modules) that both endpoints read
from. Per-request state (edition, entitlement, health, cap usage) is
NEVER computed here — that's the bearer-gated
``GET /api/v1/modules/usage`` handler's job (``saebooks/api/v1/modules.py``),
consuming ``active_flags(edition=...)`` against this registry's static
shape. This module has zero FastAPI/Request dependency on purpose so it
stays trivially testable and importable from either endpoint.

Population rules (audit §3 "Registry population rules"):

* Flag-backed, enforced (``require_feature`` present at router level):
  ``kind="flag"``, ``state="enforced"``.
* Flag-backed, unenforced (the flags in ``PLANNED_FLAGS`` below —
  originally all 11 unbacked flags from the M2 audit, now shrinking as
  each is finished and enforced; see that constant's docstring for the
  current set): ``kind="flag"``, ``state="planned"`` — listed honestly
  as roadmap, never as a live toggle (M2 §5 step 4).
* Delegated (``platform``/``capture``/``comms``/``preaccounting``):
  ``kind="delegated"``. ``entitled`` (computed in the usage endpoint,
  not here) is the OR/union of the flags the delegated service wraps at
  the caller's effective edition. ``capture`` wraps FLAG_DOCUMENT_INBOX
  / FLAG_BANK_FEEDS / FLAG_AI_EXTRACTION (genuinely tier-gated
  capabilities — hardcoding entitled=true here would leak a paid
  module's existence, exactly what 404-not-403 exists to prevent).
  ``comms`` (Wave B, 2026-07-10) wraps FLAG_SMTP_RELAY — the SAE-hosted
  transport that invoices/bills/quotes delegate to for customer-facing
  document delivery (``services/customer_email.py``); it does NOT
  represent ``services/email.py``'s core system mail (magic link,
  signup, billing receipts), which is deliberately ungated at every
  tier. ``platform`` and ``preaccounting`` wrap zero flag-gated
  capabilities (identity/signup/billing-webhook, and quotes/purchase-
  orders/time-entries' own CRUD respectively are all community-
  baseline, ungated by any FLAG_* today — note preaccounting's quotes
  DO call through to the ``comms``-wrapped send-email path, but that's
  ``comms``'s gate to represent, not preaccounting's) — an empty
  ``wrapped_flags`` therefore means unconditionally entitled, which is
  correct here (there is no paid-tier boundary being hidden when none
  exists), not the same hardcoded-true leak the audit calls out for
  capture.
* Mode (``cashbook``): ``kind="mode"``, ``entitled=True``
  unconditionally, no ``FLAG_CASHBOOK`` constant (M2 §5 step 6 — an
  always-on flag would pollute the strict-superset invariant for zero
  value; see docs/cashbook-edition-design.md).

Developer-only exclusion
-------------------------
The six developer-only flags (``FLAG_HARD_DELETE``, ``FLAG_DEV_TOOLS``,
``FLAG_EDIT_FROZEN_STATE``, ``FLAG_RAW_JSON_INSPECTOR``,
``FLAG_TENANT_SWITCHER``, ``FLAG_SKIP_AUDIT_TRAIL``) and the internal
``"developer"`` tier are excluded at the SOURCE here — ``REGISTRY``
never contains an entry for them, and ``PUBLIC_TIER_ORDER`` never
contains ``"developer"``. Both ``GET /api/v1/modules`` and
``GET /api/v1/modules/usage`` therefore inherit the filter for free by
construction, rather than each endpoint having to remember to apply it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from saebooks.services import features as _f
from saebooks.services.licence.caps import TIER_CAPS, EditionCaps

ModuleKind = Literal["flag", "delegated", "mode"]
ModuleState = Literal["enforced", "planned"]

# Published tiers only -- "developer" is internal-only (Richard's own
# instances) and must never appear in module/tier metadata shipped to a
# commercial install. See features.py's _DEVELOPER_FLAGS docstring.
PUBLIC_TIER_ORDER: tuple[str, ...] = tuple(
    tier for tier in _f.TIER_ORDER if tier != "developer"
)

# The six developer-only flags -- never surfaced in the registry.
DEVELOPER_ONLY_FLAGS: frozenset[str] = frozenset({
    _f.FLAG_HARD_DELETE,
    _f.FLAG_DEV_TOOLS,
    _f.FLAG_EDIT_FROZEN_STATE,
    _f.FLAG_RAW_JSON_INSPECTOR,
    _f.FLAG_TENANT_SWITCHER,
    _f.FLAG_SKIP_AUDIT_TRAIL,
})

# The 11 flags that exist as tier metadata but have zero backing
# enforcement code anywhere in the codebase (M2 §5 step 4 / audit §6
# module-gap list). Listed as state="planned" -- visible roadmap, never
# a live toggle. NOT a feature build; a registry-state fix only.
# Wave A (2026-07-10) enforced 4 of the original 11 -- multi_currency,
# projects_budgets, asset_v2, abr_lookup -- each already fully built with
# zero design decisions outstanding (see
# ~/records/saebooks/planned-modules-build-plan.md). Wave B (2026-07-10)
# enforced 2 more -- smtp_relay, themes. Flags are removed from this set
# one module at a time as each wave's commit lands.
# abr_lookup, multi_currency, projects_budgets, asset_v2 (Wave A),
# smtp_relay, themes (Wave B), extended_audit_modes, audit_snapshots
# (Wave C), inventory (Wave D), scheduled_backups (Wave E),
# granular_permissions (final module) all done. PLANNED_FLAGS is now
# empty -- all 11 planned modules from the M2 build-out are enforced.
#
# extended_audit_modes note (Wave C): enforced via
# services/journal.enforce_posted_edit_gate, called from BOTH the
# actually-reachable services/journal_entries.update (PATCH
# /api/v1/journal_entries/{id}) and the legacy services/journal.
# update_draft (zero live callers, kept working for its tests). The
# tier gate itself is on WRITING company.audit_mode to a non-immutable
# value (api/v1/companies.py, require_feature_inline) -- same
# conditional-gate shape as multi_currency / themes, not a router
# dependency, because the route is legitimate at every tier.
#
# audit_snapshots note (Wave C): capture (services/audit.py's
# snapshot()/snapshot_row(), called from 7 services) stays ALWAYS ON at
# every edition -- it's the point-in-time undo/recovery mechanism
# CHARTER §7.3 requires unconditionally, not the gated capability.
# Migration 0186 landed the tenant_id + FORCE RLS remediation (0055 had
# explicitly left this table unscoped as "only reachable via a
# tenant-scoped parent lookup" -- true until a direct browse existed).
# The gate is on the new browse VIEW: a router-level
# require_feature(FLAG_AUDIT_SNAPSHOTS) dependency on
# GET /api/v1/admin/audit-snapshots* (api/v1/admin.py).
#
# inventory note (Wave D): FLAG_INVENTORY gates the whole /api/v1/items
# router (CRUD + stock) via a router-level require_feature dependency —
# the conventional 404-below-tier gate. Costing method (WAC / FIFO /
# quantity_only) is an orthogonal PER-COMPANY setting
# (companies.costing_method), not a licence flag: every edition that has
# the inventory module gets to choose its method. See services/items.py.
#
# scheduled_backups note (Wave E): FLAG_SCHEDULED_BACKUPS gates the
# per-tenant logical export + client-passphrase envelope encryption
# (services/scheduled_backups.py + backup_export.py). Client-managed
# encrypted export is the open baseline; SAE-managed/guaranteed handling
# is the priced path (liability-pricing principle) — the config+runs
# tables are tenant-scoped with FORCE RLS.
#
# asset_v2 note: only the v2-specific create/update fields (diminishing
# -value model selection, tax-vs-book split) are enforced -- see
# services/assets_v2_gate.py. dispose_partial (services/assets.py) and
# the CSV bulk importer (services/assets_import.py) are real, tested
# service-layer code that Wave A found to have ZERO API/web/MCP callers
# -- there is no route to gate yet. Wire + gate those when a route
# lands; state="enforced" here reflects the fields that ARE gated, not
# a claim that every documented v2 capability has enforcement.
#
# smtp_relay note (Wave B): enforced via services/customer_email.py's
# ``sae_relay_entitled`` kwarg (a service-layer gate resolved per-request
# by the 3 send-email routers, not a router require_feature dependency)
# -- same "counts as enforced" pattern as asset_v2's assets_v2_gate.py.
# Below-Business degrades gracefully (outcome="blocked") rather than
# 404ing; see that module's docstring for why a hard route 404 was
# rejected (the /send-email routes are legitimate at every tier, only
# the SAE-hosted transport isn't). services/email.py's magic_link /
# signup / billing-receipt send paths were deliberately left UNGATED --
# those are core system mail (login, signup, billing) that must work at
# every tier, not the "invoice delivery" CHARTER §12.1 is gating.
#
# themes note (Wave B): the catalogue endpoint (GET /api/v1/themes) is a
# conventional route-level require_feature(FLAG_THEMES) 404 gate; the
# SET path (api/v1/users.py create/update) uses require_feature_inline
# the same way multi_currency gates a non-base-currency document --
# only a non-default preferred_theme crosses the tier boundary,
# "default" always works at every tier. See services/theme.py.
#
# scheduled_backups note (Wave E): every route in
# api/v1/scheduled_backups.py carries a conventional route-level
# require_feature(FLAG_SCHEDULED_BACKUPS) 404 gate (same shape as
# FLAG_SQL_TOOL's /admin/sql/execute — the closest existing precedent:
# also Pro+, also a whole-tenant-data-reach admin surface), PLUS a
# router-level admin-only dependency (Wave E's own least-privilege
# choice, not required by the flag mechanism itself). See
# services/scheduled_backups.py + services/backup_export.py (the
# per-tenant logical export, provably zero-foreign-tenant-rows) +
# services/backup_crypto.py (client-passphrase envelope encryption,
# never persisted server-side).
#
# granular_permissions note (final module, 2026-07-10): FLAG_GRANULAR_
# PERMISSIONS gates the FINE-GRAINED capability — custom roles
# (services/roles.py, /api/v1/roles) + per-permission require_permission
# enforcement (services/authz.require_permission_or_role) — at Offline+.
# Below-tier keeps whatever coarse gate a route already had (require_
# role/_require_admin, or nothing beyond require_bearer on routes that
# had no gate at all — a real pre-existing authz gap on several post/
# void-class routes, deliberately left unchanged for non-entitled
# tenants rather than silently tightened; see the module's build
# report for the explicit follow-up). Schema: roles table (tenant-
# scoped, FULL RLS checklist, migration 0190) + role_permissions wired
# to it with the D1-corrected starter grid (migration 0194) +
# UserPermission's RLS gap fixed (migration 0191, it had NO tenant_id
# at all before this module). Enforcement wired across the high-stakes
# set named in the build plan (post/void, delete-class, bas.lodge,
# user.admin, permission.manage [D4 split], settings.edit,
# reconciliation.match/unmatch, tax_code.manage, bank_account.manage)
# — the remaining ~60 routers keep their existing coarse gates,
# explicitly flagged as follow-up, not silently half-wired.
PLANNED_FLAGS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ModuleEntry:
    """One row in the module registry.

    ``flag`` — the backing ``FLAG_*`` constant for ``kind="flag"``
    entries; ``None`` otherwise.

    ``wrapped_flags`` — for ``kind="delegated"`` entries, the flags
    whose union determines ``entitled`` in the usage endpoint. An empty
    tuple means "wraps no flag-gated capability" (unconditionally
    entitled — see module docstring). Unused (empty) for ``"flag"``/
    ``"mode"`` kinds.
    """

    id: str
    label: str
    kind: ModuleKind
    group: str
    state: ModuleState
    flag: str | None = None
    wrapped_flags: tuple[str, ...] = ()


def _public_flags() -> tuple[str, ...]:
    """Every ``ALL_FLAGS`` entry except the six developer-only ones."""
    return tuple(flag for flag in _f.ALL_FLAGS if flag not in DEVELOPER_ONLY_FLAGS)


def first_public_tier_for_flag(flag: str) -> str:
    """Cheapest published tier ``flag`` first turns on at.

    Relies on the strict-superset invariant (``test_tier_superset_
    invariant``): once a flag appears in a tier's set, it's in every
    tier above it too, so the first hit walking ``PUBLIC_TIER_ORDER``
    low-to-high is unambiguous.
    """
    for tier in PUBLIC_TIER_ORDER:
        if flag in _f.tier_flags(tier):
            return tier
    raise ValueError(f"flag {flag!r} is not enabled at any published tier")


def first_public_tier_for_any(flags: tuple[str, ...]) -> str:
    """Cheapest published tier at which ANY of ``flags`` is entitled.

    Used for delegated modules' ``tier_membership`` — the tier at
    which the module first becomes useful for at least one of the
    capabilities it wraps. An empty ``flags`` tuple (wraps no
    flag-gated capability) is available from Community.
    """
    if not flags:
        return "community"
    return min(
        (first_public_tier_for_flag(flag) for flag in flags),
        key=PUBLIC_TIER_ORDER.index,
    )


# id -> (label, group). Every public (non-developer-only) flag in
# ALL_FLAGS must have an entry here -- enforced by
# tests/services/test_module_registry.py.
_FLAG_META: dict[str, tuple[str, str]] = {
    _f.FLAG_BANK_FEEDS: ("Bank feeds", "banking"),
    _f.FLAG_ABR_LOOKUP: ("ABR lookup", "integrations"),
    _f.FLAG_LEI_LOOKUP: ("LEI lookup", "integrations"),
    _f.FLAG_COMPANIES_HOUSE: ("Companies House lookup", "integrations"),
    _f.FLAG_MULTI_COMPANY: ("Multi-company", "admin"),
    _f.FLAG_EXTENDED_AUDIT_MODES: ("Extended audit modes", "compliance"),
    _f.FLAG_PER_COMPANY_SISS: ("Per-company bank credentials", "banking"),
    _f.FLAG_ATO_SBR: ("ATO SBR e-lodgement", "compliance"),
    _f.FLAG_MULTI_CURRENCY: ("Multi-currency", "accounting"),
    _f.FLAG_INVENTORY: ("Inventory", "accounting"),
    _f.FLAG_PROJECTS_BUDGETS: ("Projects & budgets", "projects"),
    _f.FLAG_ASSET_V2: ("Asset register v2", "assets"),
    _f.FLAG_GRANULAR_PERMISSIONS: ("Granular permissions", "admin"),
    _f.FLAG_THEMES: ("Themes", "settings"),
    _f.FLAG_SMTP_RELAY: ("SMTP relay", "settings"),
    _f.FLAG_STRIPE_INTEGRATION: ("Stripe integration", "integrations"),
    _f.FLAG_PAPERLESS_INTEGRATION: ("Paperless integration", "integrations"),
    _f.FLAG_QBO_IMPORT: ("QuickBooks Online import", "imports"),
    _f.FLAG_SQL_TOOL: ("Admin SQL tool", "admin"),
    _f.FLAG_AUDIT_SNAPSHOTS: ("Audit snapshots", "compliance"),
    _f.FLAG_SCHEDULED_BACKUPS: ("Scheduled backups", "admin"),
    _f.FLAG_AI_EXTRACTION: ("AI document extraction", "capture"),
    _f.FLAG_ALLOCATION_RULES: ("Overhead allocation rules", "accounting"),
    _f.FLAG_DOCUMENT_INBOX: ("Document inbox", "capture"),
    _f.FLAG_INBOX_EMAIL: ("Document inbox email-in", "capture"),
    _f.FLAG_EID_AUTH: ("Estonian eID login", "integrations"),
    _f.FLAG_ACCOUNTING_SYNC: ("Accounting-package sync", "integrations"),
    _f.FLAG_SYNC_XERO: ("Xero sync", "integrations"),
}


def _flag_state(flag: str) -> ModuleState:
    return "planned" if flag in PLANNED_FLAGS else "enforced"


def _build_flag_entries() -> tuple[ModuleEntry, ...]:
    entries = []
    for flag in _public_flags():
        label, group = _FLAG_META[flag]
        entries.append(
            ModuleEntry(
                id=flag,
                label=label,
                kind="flag",
                group=group,
                state=_flag_state(flag),
                flag=flag,
            )
        )
    return tuple(entries)


# Delegated modules -- see saebooks/services/{capture,comms,
# preaccounting,platform}_client.py for the wrapped-surface citations
# backing wrapped_flags. state="enforced" throughout: the underlying
# code is built and tested (audit §2.2); M2 does not activate live
# delegation containers (that's a separately-gated later decision per
# the audit's §7 open decisions), but the registry describes what the
# module IS, not whether delegation is currently switched on -- the
# usage endpoint (step 5) is where "not currently reachable" is
# surfaced, via health.
_DELEGATED_ENTRIES: tuple[ModuleEntry, ...] = (
    ModuleEntry(
        id="capture",
        label="Capture (document inbox, bank feeds, AI extraction)",
        kind="delegated",
        group="capture",
        state="enforced",
        wrapped_flags=(
            _f.FLAG_DOCUMENT_INBOX,
            _f.FLAG_BANK_FEEDS,
            _f.FLAG_AI_EXTRACTION,
        ),
    ),
    # Wave B (2026-07-10) / Richard's decision 7: the 4th delegated entry.
    # SMTP transport itself is delegated to the app comms module
    # (saebooks-web) -- the engine POSTs to it via
    # services/{comms_client,customer_email,email}.py and never speaks
    # SMTP directly. ``entitled`` derives purely from FLAG_SMTP_RELAY at
    # the caller's effective edition (Business+), matching the actual
    # gate in services/customer_email.py's ``sae_relay_entitled`` kwarg.
    # Note: email.py's magic_link/signup/billing-receipt sends also go
    # through the same comms module but are NOT part of what this entry
    # advertises as gated -- they're core system mail (login, signup,
    # billing) that must work at every tier, deliberately left ungated;
    # only the customer-facing invoice/bill/quote SAE-hosted delivery
    # path is the paid "SMTP relay" feature per CHARTER §12.1.
    ModuleEntry(
        id="comms",
        label="Comms (SAE-hosted email delivery)",
        kind="delegated",
        group="settings",
        state="enforced",
        wrapped_flags=(_f.FLAG_SMTP_RELAY,),
    ),
    ModuleEntry(
        id="preaccounting",
        label="Pre-accounting (quotes, purchase orders, time entries)",
        kind="delegated",
        group="accounting",
        state="enforced",
        # quotes/purchase_orders/time_entries carry no require_feature
        # gate anywhere in the codebase today -- community-baseline,
        # so there is no paid-tier boundary to leak. See module
        # docstring.
        wrapped_flags=(),
    ),
    ModuleEntry(
        id="platform",
        label="Platform (identity, signup, billing)",
        kind="delegated",
        group="core",
        state="enforced",
        # signup / login / billing-webhook are unauthenticated/all-tier
        # entry points, ungated by any FLAG_* today. See module
        # docstring.
        wrapped_flags=(),
    ),
)

# Cashbook (M2 §5 step 6) -- kind="mode": a UI mode over the ONE
# double-entry ledger (single-entry UX, no parallel storage engine),
# not a licence flag. entitled=True unconditionally at every edition
# per docs/cashbook-edition-design.md -- deliberately NO
# FLAG_CASHBOOK constant. An always-on flag would be a no-op gate that
# pollutes the strict-superset invariant (test_tier_superset_invariant)
# and its own registry entry for zero value: every tier would have to
# carry it, so it could never distinguish anything. tier_membership_for
# returns "community" for kind="mode" without needing a flag lookup.
_MODE_ENTRIES: tuple[ModuleEntry, ...] = (
    ModuleEntry(
        id="cashbook",
        label="Cashbook",
        kind="mode",
        group="core",
        state="enforced",
    ),
)

REGISTRY: tuple[ModuleEntry, ...] = (
    _build_flag_entries() + _DELEGATED_ENTRIES + _MODE_ENTRIES
)


def tier_membership_for(entry: ModuleEntry) -> str:
    """Cheapest published tier ``entry`` first appears at."""
    if entry.kind == "flag":
        assert entry.flag is not None
        return first_public_tier_for_flag(entry.flag)
    if entry.kind == "delegated":
        return first_public_tier_for_any(entry.wrapped_flags)
    # kind == "mode" -- always available.
    return "community"


def caps_matrix() -> dict[str, dict[str, object]]:
    """Static per-edition seat/company LIMITS -- ``TIER_CAPS`` minus the
    internal ``developer`` edition, shaped for JSON.

    Safe to expose unauthenticated (a comparison matrix, not a
    per-tenant usage figure) -- matches the existing ``/admin/license``
    precedent, which already renders this same table.
    """
    def _dump(caps: EditionCaps) -> dict[str, object]:
        return {
            "admin_seats": caps.admin_seats,
            "employee_seats": caps.employee_seats,
            "companies": caps.companies,
            "seat_cap_kind": caps.seat_cap_kind,
        }

    return {
        tier: _dump(TIER_CAPS[tier])
        for tier in PUBLIC_TIER_ORDER
    }
