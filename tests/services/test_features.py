"""Tests for ``saebooks.services.features``.

Covers the pure flag predicate (``is_enabled``), the display helper
(``active_flags``), and the FastAPI dependency (``require_feature``).

Edition switching is done by constructing a throwaway ``Settings``
instance for the pure tests, and by monkey-patching the module-level
singleton for the FastAPI dep test (which reads the default).
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.config import Settings
from saebooks.services.features import (
    ALL_FLAGS,
    FLAG_ABR_LOOKUP,
    FLAG_BANK_FEEDS,
    FLAG_EXTENDED_AUDIT_MODES,
    FLAG_MULTI_COMPANY,
    active_flags,
    is_enabled,
    require_feature,
)

COMMUNITY = Settings(SAEBOOKS_EDITION="community")
ENTERPRISE = Settings(SAEBOOKS_EDITION="enterprise")


# ---------------------------------------------------------------------- #
# is_enabled                                                             #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag",
    [
        FLAG_BANK_FEEDS,
        FLAG_ABR_LOOKUP,
        FLAG_MULTI_COMPANY,
        FLAG_EXTENDED_AUDIT_MODES,
    ],
)
def test_all_flags_disabled_on_community(flag: str) -> None:
    assert is_enabled(flag, settings=COMMUNITY) is False


@pytest.mark.parametrize(
    "flag",
    [
        FLAG_BANK_FEEDS,
        FLAG_ABR_LOOKUP,
        FLAG_MULTI_COMPANY,
        FLAG_EXTENDED_AUDIT_MODES,
    ],
)
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
# active_flags                                                           #
# ---------------------------------------------------------------------- #


def test_active_flags_shape_and_values() -> None:
    com = active_flags(settings=COMMUNITY)
    ent = active_flags(settings=ENTERPRISE)
    assert set(com.keys()) == set(ALL_FLAGS)
    assert set(ent.keys()) == set(ALL_FLAGS)
    assert all(v is False for v in com.values())
    assert all(v is True for v in ent.values())


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


async def test_require_feature_blocks_on_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/gated")
    assert resp.status_code == 404


async def test_require_feature_allows_on_enterprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "enterprise")
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/gated")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "true"}


# ---------------------------------------------------------------------- #
# /admin/license (integration through the real app)                      #
# ---------------------------------------------------------------------- #


async def test_admin_license_page_community(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")
    resp = await client.get("/admin/license")
    assert resp.status_code == 200
    body = resp.text
    assert "community" in body
    # Every known flag appears in the rendered matrix.
    for flag in ALL_FLAGS:
        assert flag in body
    # Community → all disabled.
    assert "disabled" in body
    assert "enabled" not in body


async def test_admin_license_page_enterprise(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "enterprise")
    resp = await client.get("/admin/license")
    assert resp.status_code == 200
    body = resp.text
    assert "enterprise" in body
    for flag in ALL_FLAGS:
        assert flag in body
    assert "enabled" in body
