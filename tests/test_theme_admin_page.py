"""Router smoke tests for /admin/theme.

Covers admin-gated GET + POST round-trip + bad-theme rejection. Uses
the ForwardAuthMiddleware upsert pattern from ``tests/test_user_roles.py``
— pre-create a user with the desired role, then send ``Remote-User``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.settings import Setting
from saebooks.models.user import User, UserRole


async def _cleanup_user(username: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.username == username))
        await session.commit()


async def _cleanup_theme_setting() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == "theme"))
        await session.commit()


@pytest.fixture
async def admin_user() -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            User(
                username=name,
                role=UserRole.ADMIN.value,
                display_name="Test Admin",
            )
        )
        await session.commit()
    try:
        yield name
    finally:
        await _cleanup_user(name)
        await _cleanup_theme_setting()


@pytest.fixture
async def readonly_user() -> str:
    name = f"readonly-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            User(
                username=name,
                role=UserRole.READONLY.value,
            )
        )
        await session.commit()
    try:
        yield name
    finally:
        await _cleanup_user(name)


async def test_theme_admin_page_renders_for_admin(
    client: AsyncClient, admin_user: str
) -> None:
    r = await client.get(
        "/admin/theme", headers={"Remote-User": admin_user}
    )
    assert r.status_code == 200
    body = r.text
    assert 'name="theme"' in body
    assert "classic" in body
    assert "default" in body


async def test_theme_admin_page_403_for_readonly(
    client: AsyncClient, readonly_user: str
) -> None:
    r = await client.get(
        "/admin/theme", headers={"Remote-User": readonly_user}
    )
    assert r.status_code == 403


async def test_theme_admin_page_401_without_user(client: AsyncClient) -> None:
    r = await client.get("/admin/theme")
    assert r.status_code == 401


async def test_theme_admin_post_persists_in_settings(
    client: AsyncClient, admin_user: str
) -> None:
    r = await client.post(
        "/admin/theme",
        headers={"Remote-User": admin_user},
        data={"theme": "classic"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Setting).where(Setting.key == "theme"))
        ).scalar_one()
    assert row.value == {"name": "classic"}
    assert row.updated_by == admin_user


async def test_theme_admin_post_rejects_unknown_theme(
    client: AsyncClient, admin_user: str
) -> None:
    r = await client.post(
        "/admin/theme",
        headers={"Remote-User": admin_user},
        data={"theme": "totally-fake"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=bad_theme" in r.headers["location"]

    # No row persisted
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Setting).where(Setting.key == "theme"))
        ).scalar_one_or_none()
    assert row is None


async def test_base_html_has_theme_assets_block(client: AsyncClient) -> None:
    """Any rendered page exercises the theme_for_request Jinja global.

    On default theme the head has no extra <link> under the /static/themes/
    path — the block evaluates to whitespace.
    """

    r = await client.get("/dashboard")
    assert r.status_code == 200
    # No classic CSS link when theme == default
    assert "/static/themes/classic/app.css" not in r.text


def test_base_html_contains_theme_block_directive() -> None:
    """The source template has the {% block theme_assets %} slot."""

    from pathlib import Path

    base_html = (
        Path(__file__).resolve().parent.parent
        / "saebooks"
        / "templates"
        / "base.html"
    ).read_text()
    assert "block theme_assets" in base_html
    assert "theme_for_request(request)" in base_html


def test_user_model_has_preferred_theme_column() -> None:
    """Column must exist + be nullable so existing rows stay valid."""

    col = User.__table__.c.preferred_theme
    assert col is not None
    assert col.nullable is True


# Hook the datetime/timezone-naive archived_at to shut sqlalchemy up on
# downgrade tests in other suites. (Unused here but kept for parity.)
_ = datetime  # type: ignore[misc]
