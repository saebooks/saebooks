"""Tests for ``saebooks.services.features``.

Covers:

* the pure flag predicate (``is_enabled``)
* the display helper (``active_flags``)
* the tier accessor (``tier_flags``)
* the superset invariant across the five editions
* the FastAPI dependency (``require_feature``)

Edition switching is done by constructing a throwaway ``Settings``
instance for the pure tests, and by monkey-patching the module-level
singleton for the FastAPI dep and integration tests (which read the
module default).
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.config import Settings
from saebooks.services.features import (
    ALL_FLAGS,
    FLAG_ABR_LOOKUP,
    FLAG_ACCOUNTING_SYNC,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
    FLAG_ASSET_V2,
    FLAG_ATO_SBR,
    FLAG_AUDIT_SNAPSHOTS,
    FLAG_BANK_FEEDS,
    FLAG_COMPANIES_HOUSE,
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_GRANULAR_PERMISSIONS,
    FLAG_INVENTORY,
    FLAG_LEI_LOOKUP,
    FLAG_MULTI_COMPANY,
    FLAG_MULTI_CURRENCY,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_PER_COMPANY_SISS,
    FLAG_PROJECTS_BUDGETS,
    FLAG_QBO_IMPORT,
    FLAG_SCHEDULED_BACKUPS,
    FLAG_SMTP_RELAY,
    FLAG_SQL_TOOL,
    FLAG_STRIPE_INTEGRATION,
    FLAG_SYNC_MYOB,
    FLAG_SYNC_QBO,
    FLAG_SYNC_XERO,
    FLAG_THEMES,
    TIER_ORDER,
    active_flags,
    is_enabled,
    require_feature,
    tier_flags,
)

COMMUNITY = Settings(SAEBOOKS_EDITION="community")
OFFLINE = Settings(SAEBOOKS_EDITION="offline")
BUSINESS = Settings(SAEBOOKS_EDITION="business")
PRO = Settings(SAEBOOKS_EDITION="pro")
ENTERPRISE = Settings(SAEBOOKS_EDITION="enterprise")


# ---------------------------------------------------------------------- #
# Tier → expected flag set                                               #
# ---------------------------------------------------------------------- #
# These fixtures are the contract. If they change, the CHARTER §12.1
# feature matrix changes — and that's a pricing/licensing decision, not
# a drive-by refactor. Keep them explicit rather than computed from the
# module so a drift between CHARTER and code shows up as a test diff.

EXPECTED_OFFLINE = frozenset({
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_MULTI_CURRENCY,
    FLAG_INVENTORY,
    FLAG_PROJECTS_BUDGETS,
    FLAG_ASSET_V2,
    FLAG_GRANULAR_PERMISSIONS,
    FLAG_THEMES,
    FLAG_SMTP_RELAY,
})

EXPECTED_BUSINESS = EXPECTED_OFFLINE | frozenset({
    FLAG_MULTI_COMPANY,
    FLAG_BANK_FEEDS,
    FLAG_ABR_LOOKUP,
    FLAG_STRIPE_INTEGRATION,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
})

EXPECTED_PRO = EXPECTED_BUSINESS | frozenset({
    FLAG_LEI_LOOKUP,
    FLAG_COMPANIES_HOUSE,
    FLAG_ATO_SBR,
    FLAG_QBO_IMPORT,
    FLAG_SQL_TOOL,
    FLAG_AUDIT_SNAPSHOTS,
    FLAG_SCHEDULED_BACKUPS,
})

EXPECTED_ENTERPRISE = EXPECTED_PRO | frozenset({
    FLAG_PER_COMPANY_SISS,
    # Build #9 — accounting-package sync. Umbrella + per-provider sub-
    # flags. All four Enterprise-only.
    FLAG_ACCOUNTING_SYNC,
    FLAG_SYNC_XERO,
    FLAG_SYNC_MYOB,
    FLAG_SYNC_QBO,
})


# ---------------------------------------------------------------------- #
# is_enabled — per-tier                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", ALL_FLAGS)
def test_all_flags_disabled_on_community(flag: str) -> None:
    assert is_enabled(flag, settings=COMMUNITY) is False


@pytest.mark.parametrize("flag", sorted(EXPECTED_OFFLINE))
def test_offline_flags_enabled_on_offline(flag: str) -> None:
    assert is_enabled(flag, settings=OFFLINE) is True


@pytest.mark.parametrize(
    "flag", sorted(frozenset(ALL_FLAGS) - EXPECTED_OFFLINE)
)
def test_non_offline_flags_disabled_on_offline(flag: str) -> None:
    assert is_enabled(flag, settings=OFFLINE) is False


@pytest.mark.parametrize("flag", sorted(EXPECTED_BUSINESS))
def test_business_flags_enabled_on_business(flag: str) -> None:
    assert is_enabled(flag, settings=BUSINESS) is True


@pytest.mark.parametrize(
    "flag", sorted(frozenset(ALL_FLAGS) - EXPECTED_BUSINESS)
)
def test_non_business_flags_disabled_on_business(flag: str) -> None:
    assert is_enabled(flag, settings=BUSINESS) is False


@pytest.mark.parametrize("flag", sorted(EXPECTED_PRO))
def test_pro_flags_enabled_on_pro(flag: str) -> None:
    assert is_enabled(flag, settings=PRO) is True


@pytest.mark.parametrize(
    "flag", sorted(frozenset(ALL_FLAGS) - EXPECTED_PRO)
)
def test_non_pro_flags_disabled_on_pro(flag: str) -> None:
    assert is_enabled(flag, settings=PRO) is False


@pytest.mark.parametrize("flag", ALL_FLAGS)
def test_all_flags_enabled_on_enterprise(flag: str) -> None:
    assert is_enabled(flag, settings=ENTERPRISE) is True


def test_is_enabled_unknown_flag_raises() -> None:
    with pytest.raises(ValueError, match="Unknown feature flag"):
        is_enabled("not_a_real_flag", settings=COMMUNITY)


def test_is_enabled_uses_default_settings_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit ``settings`` arg, fall back to the module singleton."""
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "enterprise")
    assert is_enabled(FLAG_BANK_FEEDS) is True
    monkeypatch.setattr(module_settings, "edition", "community")
    assert is_enabled(FLAG_BANK_FEEDS) is False


# ---------------------------------------------------------------------- #
# Tier superset invariant                                                #
# ---------------------------------------------------------------------- #
# CHARTER §6.2: upgrading must never remove a feature. Encode that as a
# direct test against the tier accessor so a mis-edit shows up here
# before it ships.


def test_tier_order_is_canonical() -> None:
    assert TIER_ORDER == (
        "community",
        "offline",
        "business",
        "pro",
        "enterprise",
    )


def test_tier_flags_match_expected_sets() -> None:
    assert tier_flags("community") == frozenset()
    assert tier_flags("offline") == EXPECTED_OFFLINE
    assert tier_flags("business") == EXPECTED_BUSINESS
    assert tier_flags("pro") == EXPECTED_PRO
    assert tier_flags("enterprise") == EXPECTED_ENTERPRISE


def test_tier_flags_unknown_tier_raises() -> None:
    with pytest.raises(ValueError, match="Unknown edition"):
        tier_flags("premium")


def test_tier_superset_invariant() -> None:
    """Each tier must be a strict superset of the one below it."""
    for lower, higher in zip(TIER_ORDER, TIER_ORDER[1:]):
        lower_flags = tier_flags(lower)
        higher_flags = tier_flags(higher)
        assert lower_flags <= higher_flags, (
            f"Tier '{higher}' is missing flags from '{lower}': "
            f"{sorted(lower_flags - higher_flags)}"
        )
        assert lower_flags < higher_flags, (
            f"Tier '{higher}' must add at least one flag over '{lower}'"
        )


def test_enterprise_contains_every_flag() -> None:
    """Enterprise is the always-on tier — if a flag exists, it's on here."""
    assert tier_flags("enterprise") == frozenset(ALL_FLAGS)


# ---------------------------------------------------------------------- #
# active_flags                                                           #
# ---------------------------------------------------------------------- #


def test_active_flags_shape_and_values() -> None:
    com = active_flags(settings=COMMUNITY)
    ent = active_flags(settings=ENTERPRISE)
    assert set(com.keys()) == set(ALL_FLAGS)
    assert set(ent.keys()) == set(ALL_FLAGS)
    assert all(v is False for v in com.values())
    assert all(v is True for v in ent.values())


def test_active_flags_business_mixed() -> None:
    biz = active_flags(settings=BUSINESS)
    assert biz[FLAG_BANK_FEEDS] is True
    assert biz[FLAG_MULTI_COMPANY] is True
    assert biz[FLAG_LEI_LOOKUP] is False
    assert biz[FLAG_PER_COMPANY_SISS] is False


# ---------------------------------------------------------------------- #
# require_feature                                                        #
# ---------------------------------------------------------------------- #


def test_require_feature_rejects_unknown_flag_at_decoration_time() -> None:
    with pytest.raises(ValueError, match="Unknown feature flag"):
        require_feature("bogus_flag")


def _make_app() -> FastAPI:
    """Build a tiny app with one flag-gated route."""
    app = FastAPI()

    @app.get(
        "/gated",
        dependencies=[Depends(require_feature(FLAG_BANK_FEEDS))],
    )
    async def gated() -> dict[str, str]:
        return {"ok": "true"}

    return app


@pytest.mark.parametrize("edition", ["community", "offline"])
async def test_require_feature_blocks_below_business(
    edition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAG_BANK_FEEDS is Business+ — lower tiers must 404."""
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", edition)
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/gated")
    assert resp.status_code == 404


@pytest.mark.parametrize("edition", ["business", "pro", "enterprise"])
async def test_require_feature_allows_business_and_above(
    edition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", edition)
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/gated")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "true"}


# NOTE: /admin/license HTML page tests removed in Cat-C rollup; replace with
# tests against /api/v1/admin/license when that endpoint lands.
