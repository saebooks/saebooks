"""Tests for ``saebooks.services.theme`` + ``saebooks.web``.

Covers:

* Pure :func:`resolve_theme` precedence — user > env > db_setting >
  default — across all four slots + unknown-value fall-through.
* Loader construction — :func:`build_loader` puts the active theme
  directory before the default, so a theme-side override of a
  template name wins; un-overridden names fall back.
* :func:`validate_startup_theme` raises ``ThemeError`` on unknown
  values (the config-time safety net).
* The public ``saebooks.web.templates`` is actually a
  ``Jinja2Templates`` with a multi-loader underneath and the
  ``theme_for_request`` Jinja global registered.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, FileSystemLoader

from saebooks.config import Settings
from saebooks.services import theme as theme_svc
from saebooks.services.theme import (
    ACTIVE_THEMES,
    CLASSIC_THEME,
    DEFAULT_THEME,
    ThemeError,
    build_loader,
    build_templates,
    resolve_theme,
    validate_startup_theme,
)


def _settings(frontend: str = "") -> Settings:
    """Construct a throwaway Settings with ``frontend`` set."""

    return Settings(SAEBOOKS_FRONTEND=frontend) if frontend else Settings()


# ---------------------------------------------------------------- #
# resolve_theme — precedence
# ---------------------------------------------------------------- #


def test_resolve_theme_default_when_all_empty() -> None:
    assert resolve_theme(_settings()) == DEFAULT_THEME


def test_resolve_theme_user_wins_over_env_and_db() -> None:
    s = _settings(CLASSIC_THEME)
    assert (
        resolve_theme(
            s,
            user_preferred=DEFAULT_THEME,
            db_setting=CLASSIC_THEME,
        )
        == DEFAULT_THEME
    )


def test_resolve_theme_env_wins_over_db_setting() -> None:
    s = _settings(CLASSIC_THEME)
    assert resolve_theme(s, db_setting=DEFAULT_THEME) == CLASSIC_THEME


def test_resolve_theme_db_setting_wins_over_default_when_env_empty() -> None:
    s = _settings()
    assert resolve_theme(s, db_setting=CLASSIC_THEME) == CLASSIC_THEME


def test_resolve_theme_ignores_unknown_user_value() -> None:
    """A stale preferred_theme should fall through, not blow up."""

    s = _settings(CLASSIC_THEME)
    assert resolve_theme(s, user_preferred="deprecated-v0") == CLASSIC_THEME


def test_resolve_theme_ignores_unknown_env_value() -> None:
    """A typo in SAEBOOKS_FRONTEND falls through — stricter guard lives in validate_startup_theme."""

    s = _settings("clasic-typo")
    assert resolve_theme(s, db_setting=CLASSIC_THEME) == CLASSIC_THEME


def test_resolve_theme_coerces_whitespace_and_case() -> None:
    s = _settings()
    assert resolve_theme(s, user_preferred="  CLASSIC  ") == CLASSIC_THEME


# ---------------------------------------------------------------- #
# validate_startup_theme
# ---------------------------------------------------------------- #


def test_validate_startup_theme_accepts_known() -> None:
    assert validate_startup_theme(DEFAULT_THEME) == DEFAULT_THEME
    assert validate_startup_theme(CLASSIC_THEME) == CLASSIC_THEME


def test_validate_startup_theme_empty_is_default() -> None:
    """An empty env var coerces to default — identical to unset."""

    assert validate_startup_theme("") == DEFAULT_THEME


def test_validate_startup_theme_rejects_unknown() -> None:
    with pytest.raises(ThemeError):
        validate_startup_theme("totally-not-a-theme")


# ---------------------------------------------------------------- #
# build_loader — ChoiceLoader fall-through + override precedence
# ---------------------------------------------------------------- #


def test_build_loader_default_theme_has_only_default_dir(tmp_path: Path) -> None:
    templates = tmp_path / "t"
    themes = tmp_path / "t" / "themes"
    templates.mkdir()
    themes.mkdir()

    loader = build_loader(
        templates_dir=templates,
        themes_dir=themes,
        active_theme=DEFAULT_THEME,
    )

    assert isinstance(loader, ChoiceLoader)
    assert len(loader.loaders) == 1
    assert isinstance(loader.loaders[0], FileSystemLoader)


def test_build_loader_classic_theme_prepends_theme_dir(tmp_path: Path) -> None:
    templates = tmp_path / "t"
    themes = tmp_path / "t" / "themes"
    (themes / CLASSIC_THEME).mkdir(parents=True)
    templates.mkdir(exist_ok=True)

    loader = build_loader(
        templates_dir=templates,
        themes_dir=themes,
        active_theme=CLASSIC_THEME,
    )

    assert isinstance(loader, ChoiceLoader)
    assert len(loader.loaders) == 2
    first = loader.loaders[0]
    assert isinstance(first, FileSystemLoader)
    # First loader points at themes/classic
    assert str(themes / CLASSIC_THEME) in first.searchpath


def test_build_loader_theme_override_wins_default_fallback_works(tmp_path: Path) -> None:
    """Concrete fall-through: theme has two files, default has three; the
    shared name is served from the theme dir, the others fall back."""

    templates = tmp_path / "templates"
    themes = templates / "themes"
    classic_dir = themes / CLASSIC_THEME
    templates.mkdir()
    themes.mkdir()
    classic_dir.mkdir()

    # default tree: a/base.html (default), b/unique.html (default only)
    (templates / "a").mkdir()
    (templates / "a" / "base.html").write_text("DEFAULT_BASE")
    (templates / "b").mkdir()
    (templates / "b" / "unique.html").write_text("ONLY_IN_DEFAULT")

    # classic override for base.html; nothing for unique.html
    (classic_dir / "a").mkdir()
    (classic_dir / "a" / "base.html").write_text("CLASSIC_BASE")

    loader = build_loader(
        templates_dir=templates,
        themes_dir=themes,
        active_theme=CLASSIC_THEME,
    )
    from jinja2 import Environment

    env = Environment(loader=loader)

    # Override wins.
    assert env.get_template("a/base.html").render() == "CLASSIC_BASE"
    # Fallback to default for untouched files.
    assert env.get_template("b/unique.html").render() == "ONLY_IN_DEFAULT"


def test_build_loader_rejects_unknown_active_theme(tmp_path: Path) -> None:
    templates = tmp_path / "t"
    themes = tmp_path / "t" / "themes"
    templates.mkdir()
    themes.mkdir()
    with pytest.raises(ThemeError):
        build_loader(
            templates_dir=templates,
            themes_dir=themes,
            active_theme="rubbish",
        )


# ---------------------------------------------------------------- #
# build_templates — full factory output
# ---------------------------------------------------------------- #


def test_build_templates_wires_choice_loader(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    themes = templates / "themes"
    templates.mkdir()
    themes.mkdir()
    (themes / CLASSIC_THEME).mkdir()

    t = build_templates(
        templates_dir=templates,
        themes_dir=themes,
        active_theme=CLASSIC_THEME,
    )
    assert isinstance(t.env.loader, ChoiceLoader)
    assert len(t.env.loader.loaders) == 2


# ---------------------------------------------------------------- #
# Actual app-level templates object
# ---------------------------------------------------------------- #


def test_app_templates_exposes_theme_global() -> None:
    """The shipping ``saebooks.web.templates`` has theme_for_request registered."""

    from saebooks.web import templates

    assert "theme_for_request" in templates.env.globals


def test_active_themes_contains_default_and_classic() -> None:
    assert DEFAULT_THEME in ACTIVE_THEMES
    assert CLASSIC_THEME in ACTIVE_THEMES


def test_resolve_theme_uses_config_singleton_module() -> None:
    """Sanity: module exposes everything callers need without re-export."""

    assert callable(theme_svc.resolve_theme)
    assert callable(theme_svc.validate_startup_theme)
    assert callable(theme_svc.build_templates)
