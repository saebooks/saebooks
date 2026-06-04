"""Smoke tests for the Dense theme (Batch TT — Linear / Stripe dark).

Third alternate skin alongside Classic (MYOB) and Cloud (Xero/QBO).
Dense is dark-mode-first and keyboard-centric — no sidebar, no
bottom action bar, just a thin top bar with a Command Menu pill
and a breadcrumb. Tests mirror the classic + cloud pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest

THEME_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "templates" / "themes" / "dense"
)
STATIC_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "static" / "themes" / "dense"
)


# ---------------------------------------------------------------- #
# CSS bundle
# ---------------------------------------------------------------- #


def test_dense_css_bundle_exists() -> None:
    assert (STATIC_ROOT / "app.css").is_file()


def test_dense_css_bundle_under_20kb() -> None:
    size = (STATIC_ROOT / "app.css").stat().st_size
    assert size < 20_480, f"dense app.css is {size} bytes — budget is <20KB"


def test_dense_css_has_dense_selectors() -> None:
    css = (STATIC_ROOT / "app.css").read_text()
    assert ".dn-topbar" in css
    assert ".dn-cmd" in css
    assert ".dn-kpi-strip" in css
    # Indigo is the brand accent.
    assert "--dn-indigo" in css
    # Dense is dark-mode-first — defaults are dark, light is the override.
    assert 'html[data-theme="light"]' in css


# ---------------------------------------------------------------- #
# base.html — topbar + command menu + driver
# ---------------------------------------------------------------- #


@pytest.fixture(scope="module")
def dense_base() -> str:
    return (THEME_ROOT / "base.html").read_text()


def test_dense_base_has_topbar(dense_base: str) -> None:
    """Top bar, command menu, breadcrumb strip must be present."""

    assert 'class="dn-topbar"' in dense_base
    assert 'class="dn-cmd"' in dense_base
    assert 'class="dn-crumb"' in dense_base or "dn-crumb" in dense_base
    assert "Command" in dense_base
    assert "Ctrl" in dense_base and "K" in dense_base


def test_dense_base_defaults_to_dark(dense_base: str) -> None:
    """First-visit default must be dark mode, not auto/light."""

    assert '"saebooks-theme"' in dense_base
    # The inline bootstrap falls back to "dark" (Linear pattern),
    # not "auto" (the default / classic / cloud pattern).
    assert '|| "dark"' in dense_base


def test_dense_base_has_fkey_driver(dense_base: str) -> None:
    for fkey in ('"F1"', '"F2"', '"F3"', '"F9"', '"F12"'):
        assert fkey in dense_base, f"dense driver missing {fkey}"


def test_dense_base_has_grid_keyboard_nav(dense_base: str) -> None:
    assert 'evt.key === "j"' in dense_base
    assert 'evt.key === "k"' in dense_base
    assert 'evt.key === "Enter"' in dense_base


def test_dense_base_layers_default_css_first(dense_base: str) -> None:
    default_idx = dense_base.find("/static/app.css")
    dense_idx = dense_base.find("/static/themes/dense/app.css")
    assert default_idx != -1 and dense_idx != -1
    assert default_idx < dense_idx


def test_dense_base_has_modal_slot(dense_base: str) -> None:
    assert 'id="classic-modal"' in dense_base


def test_dense_base_has_g_sequence_nav(dense_base: str) -> None:
    """The hallmark Linear/Vim g-sequence must be wired."""

    assert "gMap" in dense_base
    for dest in (
        '"/dashboard"',
        '"/journal"',
        '"/invoices"',
        '"/bills"',
        '"/contacts"',
        '"/accounts"',
        '"/reports"',
        '"/payments"',
    ):
        assert dest in dense_base


def test_dense_dashboard_override_includes_widgets() -> None:
    body = (THEME_ROOT / "dashboard" / "index.html").read_text()
    for include in (
        "_bank_balances",
        "_aged_ar",
        "_unmatched",
        "_cashflow",
        "_upcoming_recurring",
    ):
        assert include in body
    # Quick-jump panel is dense-specific.
    assert "Quick jump" in body


# ---------------------------------------------------------------- #
# Theme registry sanity
# ---------------------------------------------------------------- #


def test_dense_is_registered_active_theme() -> None:
    from saebooks.services.theme import ACTIVE_THEMES, DENSE_THEME

    assert DENSE_THEME == "dense"
    assert DENSE_THEME in ACTIVE_THEMES


def test_dense_loader_serves_dense_base_when_active() -> None:
    from jinja2 import Environment

    from saebooks.services.theme import DENSE_THEME, build_loader

    templates_dir = Path(__file__).resolve().parent.parent / "saebooks" / "templates"
    themes_dir = templates_dir / "themes"
    loader = build_loader(
        templates_dir=templates_dir,
        themes_dir=themes_dir,
        active_theme=DENSE_THEME,
    )
    env = Environment(loader=loader)
    source, _path, _uptodate = env.loader.get_source(env, "base.html")  # type: ignore[union-attr]
    assert "dn-topbar" in source
    assert "dn-cmd" in source


# ---------------------------------------------------------------- #
# Registry — all four themes now active
# ---------------------------------------------------------------- #


def test_all_four_themes_registered() -> None:
    from saebooks.services.theme import (
        ACTIVE_THEMES,
        CLASSIC_THEME,
        CLOUD_THEME,
        DEFAULT_THEME,
        DENSE_THEME,
    )

    assert frozenset(
        {DEFAULT_THEME, CLASSIC_THEME, CLOUD_THEME, DENSE_THEME}
    ) == ACTIVE_THEMES
