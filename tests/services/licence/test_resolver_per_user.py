"""Per-user resolver tests — launch-promo Pro JWT must beat the singleton.

The launch-promo flow stamps an Ed25519-signed Pro JWT on
``users.launch_promo_jwt`` after a successful signup. Without
per-user resolution every promo'd customer would still bind to the
process-wide ``settings.edition`` and silently run on Community —
the bug Richard caught after the v0.1 launch checkpoint.

These tests pin the contract:

* A user with a verifiable promo JWT resolves to Pro even when
  ``SAEBOOKS_EDITION=community``.
* A user with no JWT (or with a JWT that fails to verify) falls
  through to the singleton — never below it.
* ``resolve_licence_for_user(None)`` is identical to
  ``resolve_licence()`` (CLI / cron / unauth fallback).

Signature verification is exercised by the existing
``test_jwt.py`` suite; here we monkey-patch ``_decode_user_promo_jwt``
to inject a known ``ResolvedLicence`` and focus on the dispatch
logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from saebooks.config import settings as app_settings
from saebooks.services.licence import (
    LicenceSource,
    ResolvedLicence,
    caps_for,
    resolve_licence,
    resolve_licence_for_user,
)
from saebooks.services.licence import jwt as jwt_driver
from saebooks.services.licence import resolver as resolver_mod
from saebooks.services.licence import usb as usb_driver


@dataclass
class _FakeUser:
    """Minimal stand-in for ``saebooks.models.user.User``.

    The resolver only reads ``launch_promo_jwt`` and ``id``, so a
    plain dataclass is enough to avoid pulling in the full ORM and
    its tenant/RLS plumbing.
    """

    id: str = "user-test"
    launch_promo_jwt: str | None = None


@pytest.fixture(autouse=True)
def _reset_resolver_cache():
    resolver_mod._reset_for_tests()
    yield
    resolver_mod._reset_for_tests()


@pytest.fixture
def _community_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the process-wide singleton at Community."""
    monkeypatch.setattr(app_settings, "edition", "community")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: None)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: None)


def _pro_promo_licence() -> ResolvedLicence:
    return ResolvedLicence(
        edition="pro",
        source=LicenceSource.JWT,
        caps=caps_for("pro"),
        ledger_id="promo-test-ledger",
        licensed_to="Promo Test Co",
        expires_at=datetime.now(UTC) + timedelta(days=365),
    )


# ---------------------------------------------------------------------------
# resolve_licence_for_user — happy path: promo JWT wins over Community
# ---------------------------------------------------------------------------


def test_user_with_pro_promo_jwt_beats_community_singleton(
    _community_singleton: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact scenario the critic flagged.

    SAEBOOKS_EDITION=community + user.launch_promo_jwt=<valid Pro
    token> must resolve to Pro for that request. Otherwise 1,000
    promo customers run on Community.
    """
    # Sanity: singleton is Community.
    base = resolve_licence()
    assert base.edition == "community"

    # Stub the verifier to "accept" the JWT and yield a Pro licence.
    monkeypatch.setattr(
        resolver_mod,
        "_decode_user_promo_jwt",
        lambda token: _pro_promo_licence(),
    )

    user = _FakeUser(launch_promo_jwt="header.payload.sig")
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "pro"
    assert resolved.source is LicenceSource.JWT
    assert resolved.is_paid is True


def test_user_without_promo_jwt_falls_back_to_singleton(
    _community_singleton: None,
) -> None:
    """No JWT on the user row → use the process-wide singleton."""
    user = _FakeUser(launch_promo_jwt=None)
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "community"
    assert resolved.source is LicenceSource.COMMUNITY_FALLBACK


def test_user_with_empty_string_jwt_falls_back(
    _community_singleton: None,
) -> None:
    """Empty-string JWT (legacy / wiped) is treated as absent."""
    user = _FakeUser(launch_promo_jwt="")
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "community"


def test_resolve_licence_for_user_none_matches_singleton(
    _community_singleton: None,
) -> None:
    """user=None must behave identically to resolve_licence().

    Guards the CLI / cron / unauth callers — they pass None and
    expect the env-var/disk-cache default, not a 500.
    """
    a = resolve_licence_for_user(None)
    b = resolve_licence()
    assert a == b


# ---------------------------------------------------------------------------
# Defensive paths — bad JWT must NOT deny baseline access
# ---------------------------------------------------------------------------


def test_corrupt_promo_jwt_falls_back_to_singleton(
    _community_singleton: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad signature / malformed JWT → singleton, never lower."""
    # Pretend the verifier rejected the token.
    monkeypatch.setattr(
        resolver_mod, "_decode_user_promo_jwt", lambda token: None
    )

    user = _FakeUser(launch_promo_jwt="garbage.token.string")
    resolved = resolve_licence_for_user(user)
    # Falls through — Community singleton, NOT some fail-closed
    # "no-edition" sentinel.
    assert resolved.edition == "community"


def test_decoder_raising_exception_falls_back_to_singleton(
    _community_singleton: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhandled exception in the decoder must not 500 the request."""
    def _boom(_: str) -> ResolvedLicence | None:
        raise RuntimeError("unexpected decoder failure")

    monkeypatch.setattr(resolver_mod, "_decode_user_promo_jwt", _boom)

    user = _FakeUser(launch_promo_jwt="header.payload.sig")
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "community"  # singleton wins, not 500


# ---------------------------------------------------------------------------
# The "no portal pubkey configured" path (default in test/dev)
# ---------------------------------------------------------------------------


def test_no_portal_pubkey_configured_falls_back(
    _community_singleton: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SAEBOOKS_PORTAL_PUBKEY is unset, the in-built decoder
    returns None — and the resolver must fall back, not 500."""
    # The default test env has no PORTAL_PUBKEY set, so
    # _load_portal_public_key() returns None and _decode_user_promo_jwt
    # short-circuits to None. Don't monkey-patch — exercise the real
    # path.
    monkeypatch.setattr(jwt_driver, "PORTAL_PUBKEY_B64", "")

    user = _FakeUser(launch_promo_jwt="header.payload.sig")
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "community"


# ---------------------------------------------------------------------------
# Singleton override interaction: env-var Pro and a user with a Pro JWT —
# we should still hand back something coherent (the user's licence wins).
# ---------------------------------------------------------------------------


def test_user_promo_jwt_wins_even_when_env_overrides_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAEBOOKS_EDITION=business + user has Pro promo JWT → Pro wins.

    A self-host operator running Business via env override could
    issue promo tokens to their team; the team should get Pro.
    """
    monkeypatch.setattr(app_settings, "edition", "business")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: None)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: None)
    monkeypatch.setattr(
        resolver_mod,
        "_decode_user_promo_jwt",
        lambda token: _pro_promo_licence(),
    )

    user = _FakeUser(launch_promo_jwt="header.payload.sig")
    resolved = resolve_licence_for_user(user)
    assert resolved.edition == "pro"
