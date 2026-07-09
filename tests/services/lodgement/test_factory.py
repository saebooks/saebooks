"""Factory dispatches on LicenseService.has_feature("ato_sbr")."""
from __future__ import annotations

import pytest

from saebooks.services.licence import LicenseService
from saebooks.services.lodgement import (
    NullLodgementService,
    RemoteLodgementService,
    get_lodgement_service,
)


def test_returns_remote_when_feature_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LicenseService, "has_feature", classmethod(lambda cls, flag: True)
    )
    svc = get_lodgement_service()
    assert isinstance(svc, RemoteLodgementService)


def test_returns_null_when_feature_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LicenseService, "has_feature", classmethod(lambda cls, flag: False)
    )
    svc = get_lodgement_service()
    assert isinstance(svc, NullLodgementService)
