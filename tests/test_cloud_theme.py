"""Smoke tests for the Cloud theme (Batch SS — Xero / QBO modern).

Mirrors ``tests/test_classic_theme.py``: the CSS bundle exists and
is under budget, the cloud base template carries the left rail +
top bar + breadcrumb + main, the F-key + grid keyboard driver is
present, and the registry wiring in ``saebooks.services.theme``
knows about the theme.

We don't render the live app; file-system + template-source
checks catch the usual breakage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

THEME_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "templates" / "themes" / "cloud"
)
STATIC_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "static" / "themes" / "cloud"
)


# ---------------------------------------------------------------- #
# CSS bundle
# ---------------------------------------------------------------- #


def test_cloud_css_bundle_exists() -> None:
    assert (STATIC_ROOT / "app.css").is_file()


def test_cloud_css_bundle_under_20kb() -> None:
    """Plan target: <20KB so the first paint doesn't block on CSS."""

    size = (STATIC_ROOT / "app.css").stat().st_size
    assert size < 20_480, f"cloud app.css is {size} bytes — budget is <20KB"


def test_cloud_css_has_cloud_selectors() -> None:
    css = (STATIC_ROOT / "app.css").read_text()
    # Shell bits
    assert ".cl-rail" in css
    assert ".cl-topbar" in css
    assert ".cl-card" in css
    # Palette signal — the Xero-teal accent is the brand.
    assert "--cl-teal" in css
    # Dense-grid reskin (shared classic-grid selectors should still paint).
    assert "classic-grid" in css or "cl-grid" in css


# ---------------------------------------------------------------- #
# base.html — rail + topbar + keyboard driver
# ---------------------------------------------------------------- #


@pytest.fixture(scope="module")
def cloud_base() -> str:
    return (THEME_ROOT / "base.html").read_text()


def test_cloud_base_has_left_rail(cloud_base: str) -> None:
    """The icon rail must carry the nine primary nav entries."""

    for label in (
        "Dashboard",
        "Invoices",
        "Bills",
        "Banking",
        "Contacts",
        "Items",
        "Projects",
        "Accounting",
        "Reports",
    ):
        assert label in cloud_base, f"rail missing entry {label!r}"
    assert 'class="cl-rail"' in cloud_base


def test_cloud_base_has_topbar(cloud_base: str) -> None:
    """Top bar must carry the search widget + primary new-button."""

    assert 'class="cl-topbar"' in cloud_base
    assert 'id="cl-search-input"' in cloud_base
    # The primary CTA stays "+ New", wired to /invoices/new.
    assert "+ New" in cloud_base
    assert "/invoices/new" in cloud_base


def test_cloud_base_has_fkey_driver(cloud_base: str) -> None:
    for fkey in ('"F1"', '"F2"', '"F3"', '"F9"', '"F12"'):
        assert fkey in cloud_base, f"cloud driver missing {fkey}"


def test_cloud_base_has_grid_keyboard_nav(cloud_base: str) -> None:
    assert 'evt.key === "j"' in cloud_base
    assert 'evt.key === "k"' in cloud_base
    assert 'evt.key === "Enter"' in cloud_base


def test_cloud_base_layers_default_css_first(cloud_base: str) -> None:
    default_idx = cloud_base.find("/static/app.css")
    cloud_idx = cloud_base.find("/static/themes/cloud/app.css")
    assert default_idx != -1 and cloud_idx != -1
    assert default_idx < cloud_idx


def test_cloud_base_has_modal_slot(cloud_base: str) -> None:
    """HTMX modal slot must survive — list pages in the default tree
    already target ``#classic-modal``; cloud reuses the id."""

    assert 'id="classic-modal"' in cloud_base


def test_cloud_dashboard_override_includes_widgets() -> None:
    body = (THEME_ROOT / "dashboard" / "index.html").read_text()
    for include in (
        "_bank_balances",
        "_aged_ar",
        "_unmatched",
        "_cashflow",
        "_upcoming_recurring",
    ):
        assert include in body
    # Quick-actions panel lives on the cloud dashboard.
    assert "Quick actions" in body


# ---------------------------------------------------------------- #
# Theme registry sanity
# ---------------------------------------------------------------- #


def test_cloud_is_registered_active_theme() -> None:
    from saebooks.services.theme import ACTIVE_THEMES, CLOUD_THEME

    assert CLOUD_THEME == "cloud"
    assert CLOUD_THEME in ACTIVE_THEMES


def test_cloud_loader_serves_cloud_base_when_active() -> None:
    from jinja2 import Environment

    from saebooks.services.theme import CLOUD_THEME, build_loader

    templates_dir = Path(__file__).resolve().parent.parent / "saebooks" / "templates"
    themes_dir = templates_dir / "themes"
    loader = build_loader(
        templates_dir=templates_dir,
        themes_dir=themes_dir,
        active_theme=CLOUD_THEME,
    )
    env = Environment(loader=loader)
    source, _path, _uptodate = env.loader.get_source(env, "base.html")  # type: ignore[union-attr]
    assert "cl-rail" in source
    assert "cl-topbar" in source
