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

import itertools

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.config import Settings
from saebooks.services.features import (
    ALL_FLAGS,
    FLAG_ABR_LOOKUP,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
    FLAG_ASSET_V2,
    FLAG_ATO_SBR,
    FLAG_AUDIT_SNAPSHOTS,
    FLAG_BANK_FEEDS,
    FLAG_COMPANIES_HOUSE,
    FLAG_DEV_TOOLS,
    FLAG_DOCUMENT_INBOX,
    FLAG_EDIT_FROZEN_STATE,
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_GRANULAR_PERMISSIONS,
    FLAG_HARD_DELETE,
    FLAG_INBOX_EMAIL,
    FLAG_INVENTORY,
    FLAG_LEI_LOOKUP,
    FLAG_MULTI_COMPANY,
    FLAG_MULTI_CURRENCY,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_PER_COMPANY_SISS,
    FLAG_PROJECTS_BUDGETS,
    FLAG_QBO_IMPORT,
    FLAG_RAW_JSON_INSPECTOR,
    FLAG_SCHEDULED_BACKUPS,
    FLAG_SKIP_AUDIT_TRAIL,
    FLAG_SMTP_RELAY,
    FLAG_SQL_TOOL,
    FLAG_STRIPE_INTEGRATION,
    FLAG_TENANT_SWITCHER,
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
DEVELOPER = Settings(SAEBOOKS_EDITION="developer")


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
    # Document Inbox (issue #33) — inbox plumbing / review / manual-keyed
    # publish is pure code with zero marginal cost, so Offline and up.
    # AI extraction inside it stays gated by FLAG_AI_EXTRACTION (Business+).
    FLAG_DOCUMENT_INBOX,
    # NOTE: FLAG_SMTP_RELAY is NOT here — Wave B (2026-07-10) / Richard's
    # decision 7 moved it to Business (see EXPECTED_BUSINESS below). It
    # was mis-placed here at the v1.1 rollout: Offline is explicitly
    # no-phone-home (CHARTER §6.2), which contradicts using SAE's own
    # hosted comms relay.
})

EXPECTED_BUSINESS = EXPECTED_OFFLINE | frozenset({
    FLAG_MULTI_COMPANY,
    FLAG_BANK_FEEDS,
    FLAG_ABR_LOOKUP,
    FLAG_STRIPE_INTEGRATION,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_AI_EXTRACTION,
    FLAG_ALLOCATION_RULES,
    # Document Inbox email-in (issue #33 phase 3) — an SAE-run mailbox
    # costs real money per customer, so Business and up.
    FLAG_INBOX_EMAIL,
    # Wave B (2026-07-10) / Richard's decision 7 — CHARTER §12.1
    # "SAE-hosted SMTP for invoice delivery" is a Business-line item.
    FLAG_SMTP_RELAY,
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
})

# Developer tier — internal-only superset of enterprise + the six
# dev-only flags (hard_delete, dev_tools, edit_frozen_state,
# raw_json_inspector, tenant_switcher, skip_audit_trail). Not part of
# any billable subscription — only activated via SAEBOOKS_EDITION=developer
# on instances Richard controls directly. Developer is the new "every
# flag on" tier; enterprise stops at the public-tier boundary.
DEVELOPER_ONLY_FLAGS = frozenset({
    FLAG_HARD_DELETE,
    FLAG_DEV_TOOLS,
    FLAG_EDIT_FROZEN_STATE,
    FLAG_RAW_JSON_INSPECTOR,
    FLAG_TENANT_SWITCHER,
    FLAG_SKIP_AUDIT_TRAIL,
})

EXPECTED_DEVELOPER = EXPECTED_ENTERPRISE | DEVELOPER_ONLY_FLAGS


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


@pytest.mark.parametrize("flag", sorted(EXPECTED_ENTERPRISE))
def test_enterprise_flags_enabled_on_enterprise(flag: str) -> None:
    """Every flag in the published enterprise contract is on at enterprise.

    Note: developer-only flags (hard_delete, dev_tools, etc.) are NOT in
    EXPECTED_ENTERPRISE — they belong to the internal developer tier and
    must stay off on every commercial tier including enterprise. The
    inverse assertion lives in
    ``test_developer_only_flags_disabled_on_enterprise``.
    """
    assert is_enabled(flag, settings=ENTERPRISE) is True


@pytest.mark.parametrize("flag", sorted(DEVELOPER_ONLY_FLAGS))
def test_developer_only_flags_disabled_on_enterprise(flag: str) -> None:
    """Dev-only flags must NOT leak into the (paying) enterprise tier."""
    assert is_enabled(flag, settings=ENTERPRISE) is False


@pytest.mark.parametrize("flag", ALL_FLAGS)
def test_all_flags_enabled_on_developer(flag: str) -> None:
    """Developer is the new always-on ceiling — every known flag is on."""
    assert is_enabled(flag, settings=DEVELOPER) is True


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
        "developer",
    )


def test_tier_flags_match_expected_sets() -> None:
    assert tier_flags("community") == frozenset()
    assert tier_flags("offline") == EXPECTED_OFFLINE
    assert tier_flags("business") == EXPECTED_BUSINESS
    assert tier_flags("pro") == EXPECTED_PRO
    assert tier_flags("enterprise") == EXPECTED_ENTERPRISE
    assert tier_flags("developer") == EXPECTED_DEVELOPER


def test_tier_flags_unknown_tier_raises() -> None:
    with pytest.raises(ValueError, match="Unknown edition"):
        tier_flags("premium")


def test_tier_superset_invariant() -> None:
    """Each tier must be a strict superset of the one below it."""
    for lower, higher in itertools.pairwise(TIER_ORDER):
        lower_flags = tier_flags(lower)
        higher_flags = tier_flags(higher)
        assert lower_flags <= higher_flags, (
            f"Tier '{higher}' is missing flags from '{lower}': "
            f"{sorted(lower_flags - higher_flags)}"
        )
        assert lower_flags < higher_flags, (
            f"Tier '{higher}' must add at least one flag over '{lower}'"
        )


def test_developer_contains_every_flag() -> None:
    """Developer is the always-on ceiling — if a flag exists, it's on here.

    Replaces the prior ``test_enterprise_contains_every_flag`` invariant:
    when the developer tier was added above enterprise, enterprise lost
    its "every flag" property by design. The dev-only flags must NOT
    ship in any commercial tier, so the always-on assertion now belongs
    to developer.
    """
    assert tier_flags("developer") == frozenset(ALL_FLAGS)


def test_enterprise_is_developer_minus_dev_only_flags() -> None:
    """Enterprise = developer minus the six dev-only flags. Pins the
    invariant that the only difference between the two top tiers is
    the developer-only set — any new flag added to enterprise that
    isn't also added to developer would break the superset invariant
    and surface here."""
    assert tier_flags("enterprise") == tier_flags("developer") - DEVELOPER_ONLY_FLAGS


# ---------------------------------------------------------------------- #
# active_flags                                                           #
# ---------------------------------------------------------------------- #


def test_active_flags_shape_and_values() -> None:
    com = active_flags(settings=COMMUNITY)
    ent = active_flags(settings=ENTERPRISE)
    dev = active_flags(settings=DEVELOPER)
    # Shape: every known flag is keyed on every tier (display matrix
    # contract — /admin/license renders rows for every flag).
    assert set(com.keys()) == set(ALL_FLAGS)
    assert set(ent.keys()) == set(ALL_FLAGS)
    assert set(dev.keys()) == set(ALL_FLAGS)
    # Community: nothing on.
    assert all(v is False for v in com.values())
    # Enterprise: public-tier flags on, dev-only flags off.
    for flag in EXPECTED_ENTERPRISE:
        assert ent[flag] is True, f"enterprise should enable {flag}"
    for flag in DEVELOPER_ONLY_FLAGS:
        assert ent[flag] is False, f"enterprise must not enable dev-only {flag}"
    # Developer: every known flag on (always-on ceiling).
    assert all(v is True for v in dev.values())


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


# ---------------------------------------------------------------------- #
# require_feature — per-user (launch promo) resolution                   #
# ---------------------------------------------------------------------- #
# These tests pin the launch-promo critic fix: a user with a Pro JWT
# stamped on users.launch_promo_jwt must clear a Pro-flag gate even
# when the singleton is Community. Without per-user resolution every
# promo'd customer would silently run on Community.


@pytest.mark.parametrize(
    "flag,user_edition,want_status",
    [
        # User with Pro promo JWT clears a Business+ flag on Community.
        ("bank_feeds", "pro", 200),
        # User with Pro promo JWT clears a Pro+ flag on Community.
        ("ato_sbr", "pro", 200),
        # No user (unauthenticated path) must still respect singleton —
        # Community → Business flag blocked.
        ("bank_feeds", None, 404),
    ],
)
async def test_require_feature_consults_per_user_promo_jwt(
    flag: str,
    user_edition: str | None,
    want_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-user override beats Community singleton.

    Mirrors the v0.1 launch scenario: SAEBOOKS_EDITION=community on
    the SaaS box; first-1000 users carry a Pro JWT on their row;
    every gated Pro/Business route should let them in.
    """
    from saebooks.config import settings as module_settings
    from saebooks.services.features import require_feature
    from saebooks.services.licence import (
        LicenceSource,
        ResolvedLicence,
        caps_for,
    )
    from saebooks.services.licence import resolver as resolver_mod

    monkeypatch.setattr(module_settings, "edition", "community")

    # Stub the per-user resolver to behave as if the request user has
    # a verified Pro JWT (or no JWT at all if user_edition is None).
    def _fake_for_user(u):
        if u is None or user_edition is None:
            # singleton fallback
            return ResolvedLicence(
                edition="community",
                source=LicenceSource.COMMUNITY_FALLBACK,
                caps=caps_for("community"),
            )
        return ResolvedLicence(
            edition=user_edition,
            source=LicenceSource.JWT,
            caps=caps_for(user_edition),
        )

    monkeypatch.setattr(
        resolver_mod, "resolve_licence_for_user", _fake_for_user
    )

    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        # Tests inject "X-Test-User: 1" to simulate a logged-in
        # request whose user row carries a Pro promo JWT. Absence
        # leaves request.state.user unset, exercising the fallback.
        if request.headers.get("X-Test-User"):
            class _U:
                id = "test-user"
                launch_promo_jwt = "header.payload.sig"
            request.state.user = _U()
        return await call_next(request)

    @app.get(
        "/gated",
        dependencies=[Depends(require_feature(flag))],
    )
    async def gated() -> dict[str, str]:
        return {"ok": "true"}

    headers = {"X-Test-User": "1"} if user_edition is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/gated", headers=headers)
    assert resp.status_code == want_status, (
        f"flag={flag} user_edition={user_edition} → {resp.status_code}"
    )


# ---------------------------------------------------------------------- #
# feature_enabled_for_request (Wave B) — non-raising per-request check   #
# ---------------------------------------------------------------------- #
# Added for services/customer_email.py's ``sae_relay_entitled`` gate: a
# below-Business tenant's /send-email attempt is legitimate at every
# tier (only the SAE-hosted transport isn't), so the call site needs a
# bool to branch on rather than a 404. Same resolution as
# require_feature/require_feature_inline, minus the raise.


def test_feature_enabled_for_request_rejects_unknown_flag() -> None:
    from starlette.requests import Request

    from saebooks.services.features import feature_enabled_for_request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = Request(scope)
    with pytest.raises(ValueError, match="Unknown feature flag"):
        feature_enabled_for_request("bogus_flag", request)


@pytest.mark.parametrize(
    "edition,want",
    [("community", False), ("offline", False), ("business", True), ("pro", True)],
)
async def test_feature_enabled_for_request_resolves_singleton_edition(
    edition: str, want: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``request.state.user`` (unauthenticated / no promo JWT) falls
    back to the process-wide singleton edition — same as require_feature."""
    from saebooks.config import settings as module_settings
    from saebooks.services.features import feature_enabled_for_request

    monkeypatch.setattr(module_settings, "edition", edition)

    from starlette.requests import Request as StarletteRequest

    # A bare request with no ``state.user`` — _effective_edition_for_request
    # reads it via getattr(request.state, "user", None) and falls back to the
    # process-wide singleton edition (monkeypatched above). Construct the
    # Request directly rather than through a throwaway FastAPI app, so the
    # test exercises the helper deterministically without depending on
    # FastAPI's request-parameter injection.
    request = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )
    assert feature_enabled_for_request(FLAG_SMTP_RELAY, request) is want


# NOTE: /admin/license HTML page tests removed in Cat-C rollup; replace with
# tests against /api/v1/admin/license when that endpoint lands.
