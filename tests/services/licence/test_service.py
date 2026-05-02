"""Tests for ``LicenseService`` facade.

The facade is thin — most behaviour is exercised through ``test_resolver``
and ``test_features``. Here we just guard the public contract called out
in the saebooks-infrastructure plan §8 build #1: ``has_feature(name)``
and ``snapshot()``.
"""
from __future__ import annotations

import pytest

from saebooks.services.licence import (
    LicenseService,
    LicenseSnapshot,
    resolver,
)


@pytest.fixture(autouse=True)
def _reset_resolver():
    resolver._reset_for_tests()
    yield
    resolver._reset_for_tests()


def test_has_feature_true_for_active_flag(monkeypatch):
    monkeypatch.setattr(
        "saebooks.config.settings.edition", "business", raising=True
    )
    assert LicenseService.has_feature("bank_feeds") is True


def test_has_feature_false_for_higher_tier(monkeypatch):
    monkeypatch.setattr(
        "saebooks.config.settings.edition", "business", raising=True
    )
    # ato_sbr is Pro+, not Business.
    assert LicenseService.has_feature("ato_sbr") is False


def test_has_feature_unknown_flag_raises():
    with pytest.raises(ValueError):
        LicenseService.has_feature("not_a_real_flag")


def test_snapshot_shape(monkeypatch):
    monkeypatch.setattr(
        "saebooks.config.settings.edition", "community", raising=True
    )
    snap = LicenseService.snapshot()
    assert isinstance(snap, LicenseSnapshot)
    assert snap.edition == "community"
    assert snap.is_paid is False
    assert snap.is_perpetual is False


def test_reload_recomputes(monkeypatch):
    monkeypatch.setattr(
        "saebooks.config.settings.edition", "community", raising=True
    )
    LicenseService.snapshot()
    monkeypatch.setattr(
        "saebooks.config.settings.edition", "pro", raising=True
    )
    snap = LicenseService.reload()
    assert snap.edition == "pro"
