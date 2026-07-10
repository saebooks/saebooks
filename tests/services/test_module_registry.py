"""Tests for ``saebooks.services.module_registry`` (M2 §5 steps 3-6).

Covers:

* every public flag has static metadata and appears exactly once in
  ``REGISTRY``;
* the 11 unbacked flags are marked ``state="planned"``, everything else
  ``state="enforced"``;
* the six developer-only flags + the internal "developer" tier never
  appear anywhere in the registry;
* delegated modules' wrapped-flag sets and ``tier_membership``
  resolution (including the empty-wrapped-flags "always entitled"
  case for platform/preaccounting);
* ``caps_matrix()`` excludes the developer edition.
"""
from __future__ import annotations

import pytest

from saebooks.services import features as f
from saebooks.services.module_registry import (
    DEVELOPER_ONLY_FLAGS,
    PLANNED_FLAGS,
    PUBLIC_TIER_ORDER,
    REGISTRY,
    caps_matrix,
    first_public_tier_for_any,
    first_public_tier_for_flag,
    tier_membership_for,
)

# ---------------------------------------------------------------------- #
# Public-tier / developer-exclusion invariants                          #
# ---------------------------------------------------------------------- #


def test_public_tier_order_excludes_developer() -> None:
    assert "developer" not in PUBLIC_TIER_ORDER
    assert PUBLIC_TIER_ORDER == ("community", "offline", "business", "pro", "enterprise")


def test_developer_only_flags_have_no_registry_entry() -> None:
    registry_ids = {entry.id for entry in REGISTRY}
    for flag in DEVELOPER_ONLY_FLAGS:
        assert flag not in registry_ids, f"dev-only flag {flag!r} leaked into REGISTRY"


def test_developer_only_flags_have_no_registry_flag_reference() -> None:
    """Belt-and-braces: no entry's ``.flag`` points at a dev-only flag
    either (guards a future entry added by ``id`` alone but backed by
    the wrong constant)."""
    for entry in REGISTRY:
        if entry.flag is not None:
            assert entry.flag not in DEVELOPER_ONLY_FLAGS


# ---------------------------------------------------------------------- #
# Flag coverage — every public flag is represented exactly once         #
# ---------------------------------------------------------------------- #


def test_every_public_flag_has_exactly_one_registry_entry() -> None:
    public_flags = {flag for flag in f.ALL_FLAGS if flag not in DEVELOPER_ONLY_FLAGS}
    flag_entries = [entry for entry in REGISTRY if entry.kind == "flag"]
    registry_flags = [entry.flag for entry in flag_entries]
    assert set(registry_flags) == public_flags
    assert len(registry_flags) == len(set(registry_flags)), "duplicate flag entry in REGISTRY"


@pytest.mark.parametrize("flag", sorted(PLANNED_FLAGS))
def test_planned_flags_are_state_planned_in_registry(flag: str) -> None:
    entry = next(e for e in REGISTRY if e.flag == flag)
    assert entry.state == "planned"


@pytest.mark.parametrize(
    "flag",
    sorted(
        {f_ for f_ in f.ALL_FLAGS if f_ not in DEVELOPER_ONLY_FLAGS} - PLANNED_FLAGS
    ),
)
def test_non_planned_public_flags_are_state_enforced(flag: str) -> None:
    entry = next(e for e in REGISTRY if e.flag == flag)
    assert entry.state == "enforced"


def test_planned_flags_matches_the_remaining_unbacked_flags() -> None:
    """Pins the exact set named in the M2 spec, minus each flag Wave A/B/C
    (2026-07-10) enforces as it lands — a drift here is a
    licensing/roadmap decision, not a drive-by refactor.

    Wave A order: abr_lookup (done), multi_currency (done),
    projects_budgets (done), asset_v2 (done -- diminishing-value model
    selection + tax-vs-book split fields only; dispose_partial and CSV
    bulk-import are unwired, see services/assets_v2_gate.py).
    Wave B order: smtp_relay (done -- tier move to Business + gated via
    services/customer_email.py's sae_relay_entitled kwarg), themes
    (done -- allow-list + FLAG_THEMES gate, see services/theme.py).
    Wave C: extended_audit_modes (done -- company.audit_mode is the
    single source of truth, gated via require_feature_inline on the
    PATCH /companies write path; enforcement in
    services/journal.enforce_posted_edit_gate), audit_snapshots (done --
    tenant_id + FORCE RLS migration, browse API gated via require_feature
    on GET /api/v1/admin/audit-snapshots*).
    Wave D: inventory (done -- multi-method costing as a per-company
    setting; FLAG_INVENTORY gates the /api/v1/items router, see
    services/items.py).
    Wave E: scheduled_backups (done -- per-tenant logical export +
    client-passphrase envelope encryption + FLAG_SCHEDULED_BACKUPS gate,
    see services/scheduled_backups.py + services/backup_export.py).
    granular_permissions (final module, done -- fine-grained
    require_permission enforcement + tenant-scoped custom roles at
    Offline+, see services/authz.require_permission_or_role +
    services/roles.py + /api/v1/roles). PLANNED_FLAGS is now EMPTY --
    all 11 planned modules from the M2 build-out are enforced.
    """
    assert frozenset() == PLANNED_FLAGS
    assert len(PLANNED_FLAGS) == 0


# ---------------------------------------------------------------------- #
# Delegated modules                                                      #
# ---------------------------------------------------------------------- #


def test_capture_wraps_the_three_capture_flags() -> None:
    capture = next(e for e in REGISTRY if e.id == "capture")
    assert capture.kind == "delegated"
    assert set(capture.wrapped_flags) == {
        f.FLAG_DOCUMENT_INBOX,
        f.FLAG_BANK_FEEDS,
        f.FLAG_AI_EXTRACTION,
    }


def test_comms_wraps_smtp_relay_only() -> None:
    """Wave B's 4th delegated entry -- SMTP transport is delegated to the
    app comms module; entitled derives from FLAG_SMTP_RELAY alone."""
    comms = next(e for e in REGISTRY if e.id == "comms")
    assert comms.kind == "delegated"
    assert comms.state == "enforced"
    assert set(comms.wrapped_flags) == {f.FLAG_SMTP_RELAY}


def test_comms_tier_membership_is_business() -> None:
    """Pins the Wave B tier move: smtp_relay (and therefore comms) first
    turns on at Business, not Offline."""
    comms = next(e for e in REGISTRY if e.id == "comms")
    assert tier_membership_for(comms) == "business"


@pytest.mark.parametrize("module_id", ["platform", "preaccounting"])
def test_platform_and_preaccounting_wrap_no_flag(module_id: str) -> None:
    """Both delegate community-baseline capability wrapped by no
    require_feature gate anywhere in the codebase -- empty
    wrapped_flags is correct (not the hardcoded-true leak the audit
    flags for capture), see module docstring."""
    entry = next(e for e in REGISTRY if e.id == module_id)
    assert entry.kind == "delegated"
    assert entry.wrapped_flags == ()


def test_delegated_entries_are_state_enforced() -> None:
    for entry in REGISTRY:
        if entry.kind == "delegated":
            assert entry.state == "enforced"


# ---------------------------------------------------------------------- #
# tier_membership_for / first_public_tier_for_*                         #
# ---------------------------------------------------------------------- #


def test_first_public_tier_for_flag_matches_known_tiers() -> None:
    assert first_public_tier_for_flag(f.FLAG_DOCUMENT_INBOX) == "offline"
    assert first_public_tier_for_flag(f.FLAG_BANK_FEEDS) == "business"
    assert first_public_tier_for_flag(f.FLAG_ATO_SBR) == "pro"
    assert first_public_tier_for_flag(f.FLAG_PER_COMPANY_SISS) == "enterprise"
    # Wave B tier move: smtp_relay is Business+, not Offline+.
    assert first_public_tier_for_flag(f.FLAG_SMTP_RELAY) == "business"
    # Wave B: themes stays Offline+ (unchanged by the smtp_relay move).
    assert first_public_tier_for_flag(f.FLAG_THEMES) == "offline"


def test_first_public_tier_for_any_empty_is_community() -> None:
    assert first_public_tier_for_any(()) == "community"


def test_first_public_tier_for_any_picks_cheapest() -> None:
    # document_inbox=offline, bank_feeds=business, ai_extraction=business
    # -> cheapest is offline.
    assert first_public_tier_for_any(
        (f.FLAG_DOCUMENT_INBOX, f.FLAG_BANK_FEEDS, f.FLAG_AI_EXTRACTION)
    ) == "offline"


def test_tier_membership_for_flag_entry() -> None:
    entry = next(e for e in REGISTRY if e.id == f.FLAG_BANK_FEEDS)
    assert tier_membership_for(entry) == "business"


def test_tier_membership_for_delegated_capture_is_offline() -> None:
    capture = next(e for e in REGISTRY if e.id == "capture")
    assert tier_membership_for(capture) == "offline"


def test_tier_membership_for_delegated_platform_is_community() -> None:
    platform = next(e for e in REGISTRY if e.id == "platform")
    assert tier_membership_for(platform) == "community"


def test_tier_membership_never_returns_developer() -> None:
    for entry in REGISTRY:
        assert tier_membership_for(entry) != "developer"


# ---------------------------------------------------------------------- #
# caps_matrix                                                            #
# ---------------------------------------------------------------------- #


def test_caps_matrix_excludes_developer_edition() -> None:
    matrix = caps_matrix()
    assert "developer" not in matrix
    assert set(matrix.keys()) == set(PUBLIC_TIER_ORDER)


def test_caps_matrix_shape() -> None:
    matrix = caps_matrix()
    business = matrix["business"]
    assert business == {
        "admin_seats": 2,
        "employee_seats": 3,
        "companies": 2,
        "seat_cap_kind": "hard",
    }
    enterprise = matrix["enterprise"]
    assert enterprise["admin_seats"] is None  # unlimited
