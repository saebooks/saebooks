"""Tests for ``saebooks.services.licence.resolver``.

Boot-time resolution is entirely driven by the three side inputs:

1. ``settings.edition`` env override (SAEBOOKS_EDITION).
2. ``usb.load_usb_licence`` return value.
3. ``jwt.load_portal_jwt`` return value.

Every test here monkey-patches those three inputs to exercise the
four code paths:

* env override wins;
* USB wins over JWT;
* JWT wins over fallback;
* fallback → community.
"""
from __future__ import annotations

import pytest

from saebooks.services.licence import (
    LicenceSource,
    ResolvedLicence,
    caps_for,
    resolve_licence,
)
from saebooks.services.licence import jwt as jwt_driver
from saebooks.services.licence import resolver as resolver_mod
from saebooks.services.licence import usb as usb_driver


@pytest.fixture(autouse=True)
def _reset_resolver_cache() -> None:
    """Every test starts with an empty resolver cache."""
    resolver_mod._reset_for_tests()
    yield
    resolver_mod._reset_for_tests()


def test_community_fallback_when_nothing_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod._settings, "edition", "community")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: None)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: None)

    r = resolve_licence()
    assert r.edition == "community"
    assert r.source is LicenceSource.COMMUNITY_FALLBACK
    assert r.caps == caps_for("community")
    assert r.is_paid is False


def test_env_override_short_circuits_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAEBOOKS_EDITION=enterprise skips both drivers, logs a warning."""
    monkeypatch.setattr(resolver_mod._settings, "edition", "enterprise")
    # Drivers shouldn't even be called.
    def _boom() -> None:
        raise AssertionError("drivers must not be called when env overrides")
    monkeypatch.setattr(usb_driver, "load_usb_licence", _boom)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", _boom)

    r = resolve_licence()
    assert r.edition == "enterprise"
    assert r.caps == caps_for("enterprise")


def test_usb_takes_precedence_over_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod._settings, "edition", "community")
    fake_usb = usb_driver.build_fake_licence_for_tests(edition="offline")
    fake_jwt = jwt_driver.build_fake_licence_for_tests(edition="business")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: fake_usb)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: fake_jwt)

    r = resolve_licence()
    assert r.edition == "offline"
    assert r.source is LicenceSource.USB
    assert r.is_perpetual is True
    assert r.is_paid is True


def test_jwt_used_when_usb_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod._settings, "edition", "community")
    fake_jwt = jwt_driver.build_fake_licence_for_tests(edition="pro")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: None)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: fake_jwt)

    r = resolve_licence()
    assert r.edition == "pro"
    assert r.source is LicenceSource.JWT
    assert r.caps == caps_for("pro")
    assert r.is_perpetual is False
    assert r.is_paid is True


def test_resolution_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod._settings, "edition", "community")
    call_count = {"usb": 0, "jwt": 0}

    def _count_usb() -> ResolvedLicence | None:
        call_count["usb"] += 1
        return None

    def _count_jwt() -> ResolvedLicence | None:
        call_count["jwt"] += 1
        return None

    monkeypatch.setattr(usb_driver, "load_usb_licence", _count_usb)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", _count_jwt)

    resolve_licence()
    resolve_licence()
    resolve_licence()
    assert call_count == {"usb": 1, "jwt": 1}


def test_force_reruns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod._settings, "edition", "community")
    monkeypatch.setattr(usb_driver, "load_usb_licence", lambda: None)
    monkeypatch.setattr(jwt_driver, "load_portal_jwt", lambda: None)

    first = resolve_licence()
    second = resolve_licence(force=True)
    # Equal by value, but re-resolved.
    assert first == second
