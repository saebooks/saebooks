"""Smoke tests for the MYOB Classic theme (Batch RR).

The ChoiceLoader is already covered in ``tests/services/test_theme.py``;
this module exercises the actual classic theme directory — the CSS
bundle exists and is the target size, the classic base template has
the sidebar / status bar / F-key driver, and the list overrides carry
the ``data-new-action`` hook that the F2 keyboard shortcut binds to.

We don't render the classic theme through the live app (that'd need a
module-level theme swap + container rebuild); the sanity targets here
are file-system + template-source checks, which are enough to catch a
missing asset or a malformed sidebar.
"""
from __future__ import annotations

from pathlib import Path

import pytest

THEME_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "templates" / "themes" / "classic"
)
STATIC_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "static" / "themes" / "classic"
)


# ---------------------------------------------------------------- #
# CSS bundle
# ---------------------------------------------------------------- #


def test_classic_css_bundle_exists() -> None:
    assert (STATIC_ROOT / "app.css").is_file()


def test_classic_css_bundle_under_20kb() -> None:
    """Plan target: <20KB so first paint doesn't block on CSS."""

    size = (STATIC_ROOT / "app.css").stat().st_size
    assert size < 20_480, f"classic app.css is {size} bytes — budget is <20KB"


def test_classic_css_has_classic_grid_selector() -> None:
    css = (STATIC_ROOT / "app.css").read_text()
    assert "table.classic-grid" in css
    assert "--c-primary" in css
    assert "classic-sidebar" in css
    assert "classic-statusbar" in css


# ---------------------------------------------------------------- #
# base.html — sidebar + status bar + F-key driver
# ---------------------------------------------------------------- #


@pytest.fixture(scope="module")
def classic_base() -> str:
    return (THEME_ROOT / "base.html").read_text()


def test_classic_base_has_sidebar_group_headers(classic_base: str) -> None:
    """Every section of the sidebar tree must be present."""

    for marker in (
        "Workspace",
        "Sales",
        "Purchases",
        "Contacts &amp; items",
        "Banking",
        "Accounting",
        "Admin",
    ):
        assert marker in classic_base, f"sidebar missing group {marker!r}"


def test_classic_base_has_statusbar(classic_base: str) -> None:
    """Status bar must reference user + edition + the hot-key hints."""

    assert 'class="classic-statusbar"' in classic_base
    assert "edition" in classic_base
    assert "Ctrl" in classic_base and "F1" in classic_base and "F2" in classic_base


def test_classic_base_has_fkey_driver(classic_base: str) -> None:
    """The plan prescribes F1/F2/F3/F5/F9/F12 bindings."""

    for fkey in ('"F1"', '"F2"', '"F3"', '"F5"', '"F9"', '"F12"'):
        assert fkey in classic_base, f"F-key driver missing {fkey}"


def test_classic_base_has_grid_keyboard_nav(classic_base: str) -> None:
    """j/k row navigation + Enter opens, per the MYOB UX."""

    assert 'evt.key === "j"' in classic_base
    assert 'evt.key === "k"' in classic_base
    assert 'evt.key === "Enter"' in classic_base


def test_classic_base_keeps_gsequence_nav(classic_base: str) -> None:
    """Two-key g-nav from the default theme must survive in classic."""

    assert "gMap" in classic_base
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
        assert dest in classic_base


def test_classic_base_layers_default_css_first(classic_base: str) -> None:
    """Default bundle loads first so non-overridden pages still paint
    usefully; classic.css stacks after to override dense grids + nav.
    """

    default_idx = classic_base.find("/static/app.css")
    classic_idx = classic_base.find("/static/themes/classic/app.css")
    assert default_idx != -1 and classic_idx != -1
    assert default_idx < classic_idx


def test_classic_base_loads_classic_css(classic_base: str) -> None:
    assert '/static/themes/classic/app.css' in classic_base


def test_classic_base_has_modal_slot(classic_base: str) -> None:
    """RR2 uses hx-target='#classic-modal' for in-page editing."""

    assert 'id="classic-modal"' in classic_base
    assert 'class="classic-modal"' in classic_base


# ---------------------------------------------------------------- #
# List overrides
# ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relpath,new_action",
    [
        ("invoices/list.html", "/invoices/new"),
        ("bills/list.html", "/bills/new"),
        ("contacts/list.html", "/contacts/new"),
        ("journal/list.html", "/journal/new"),
    ],
)
def test_classic_list_override_carries_data_new_action(
    relpath: str, new_action: str
) -> None:
    """F2 + 'n' shortcuts read the first [data-new-action] on the page."""

    body = (THEME_ROOT / relpath).read_text()
    assert f'data-new-action="{new_action}"' in body
    # Classic theme renders dense grids via classic-grid; plain list
    # pages switch over to it.
    assert "classic-grid" in body


def test_classic_reports_index_override_exists() -> None:
    """Reports index gets classic card chrome."""

    body = (THEME_ROOT / "reports" / "index.html").read_text()
    assert 'class="dashboard-grid"' in body
    assert 'class="card"' in body
    # All 10 report links still land.
    for href in (
        "/reports/trial-balance",
        "/reports/profit-loss",
        "/reports/balance-sheet",
        "/reports/bas",
        "/reports/aged-ar",
        "/reports/aged-ap",
        "/reports/pl-by-segment",
        "/reports/budget-vs-actual",
        "/reports/cashflow-forecast",
        "/reports/close-year",
    ):
        assert href in body


def test_classic_dashboard_override_includes_widgets() -> None:
    body = (THEME_ROOT / "dashboard" / "index.html").read_text()
    for include in (
        "_bank_balances",
        "_aged_ar",
        "_unmatched",
        "_cashflow",
        "_upcoming_recurring",
    ):
        assert include in body


# ---------------------------------------------------------------- #
# Theme registry sanity
# ---------------------------------------------------------------- #


def test_classic_is_registered_active_theme() -> None:
    from saebooks.services.theme import ACTIVE_THEMES, CLASSIC_THEME

    assert CLASSIC_THEME == "classic"
    assert CLASSIC_THEME in ACTIVE_THEMES


def test_classic_loader_serves_classic_base_when_active(tmp_path: Path) -> None:
    """End-to-end: build_loader with active_theme=classic + our real
    ``saebooks/templates`` tree serves the classic base.html over the
    flat one."""

    from jinja2 import Environment

    from saebooks.services.theme import CLASSIC_THEME, build_loader

    templates_dir = Path(__file__).resolve().parent.parent / "saebooks" / "templates"
    themes_dir = templates_dir / "themes"
    loader = build_loader(
        templates_dir=templates_dir,
        themes_dir=themes_dir,
        active_theme=CLASSIC_THEME,
    )
    env = Environment(loader=loader)
    # We don't render (render needs the theme_for_request global);
    # finding the source is enough to prove the ChoiceLoader picks
    # the classic file.
    source, _path, _uptodate = env.loader.get_source(env, "base.html")  # type: ignore[union-attr]
    assert "classic-sidebar" in source
    assert "classic-statusbar" in source
