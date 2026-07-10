"""Contract tests for ``GET /api/v1/themes`` (Wave B / FLAG_THEMES).

Covers:
* bearer required (401)
* route-level require_feature(FLAG_THEMES) gate — 404 below Offline
* full catalogue (id + label, including "default") at Offline+
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app
from saebooks.services.theme import ACTIVE_THEMES, DEFAULT_THEME_ID

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_themes_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/themes")
    assert r.status_code == 401


@pytest.mark.parametrize("edition", ["community"])
async def test_themes_404_below_offline(
    edition: str, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", edition)

    r = await api_client.get("/api/v1/themes")
    assert r.status_code == 404


@pytest.mark.parametrize("edition", ["offline", "business", "pro", "enterprise"])
async def test_themes_200_at_offline_and_above(
    edition: str, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", edition)

    r = await api_client.get("/api/v1/themes")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body}
    assert ids == ACTIVE_THEMES
    assert DEFAULT_THEME_ID in ids
    for item in body:
        assert "id" in item
        assert "label" in item
