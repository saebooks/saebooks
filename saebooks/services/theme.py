"""Theme selection — pure helpers + the ``ChoiceLoader``-backed template factory.

Why this module:

SAE Books ships with a single default Jinja tree at ``saebooks/templates/``.
Batches QQ/RR introduce an optional **MYOB Classic** theme layer that can
override any template (e.g. ``dashboard/index.html``) without duplicating
the rest of the tree. The active theme is a deployment-wide knob
(``SAEBOOKS_FRONTEND`` env / DB setting) that resolves at app startup;
per-user ``preferred_theme`` overrides the *CSS bundle* loaded in the
page head but does **not** swap templates (the Jinja ``ChoiceLoader`` is
constructed once per process).

Resolution precedence when asking "which theme for this request":

    user.preferred_theme  >  SAEBOOKS_FRONTEND env  >  settings.theme DB row  >  DEFAULT_THEME

The first three are validated against :data:`ACTIVE_THEMES` — unknown
values fall through rather than raising, so a stale DB row from a
removed theme doesn't 500 the whole app. The app-startup path is
stricter: :func:`validate_startup_theme` raises ``ValueError`` on
unknown env values so typos fail loud at ``create_app`` time.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

if TYPE_CHECKING:
    from saebooks.config import Settings

# ---------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------- #

DEFAULT_THEME = "default"
CLASSIC_THEME = "classic"

#: Every theme known to the server. ``default`` is the flat
#: ``saebooks/templates/`` tree; every other entry must have a directory
#: under ``saebooks/templates/themes/<name>/`` containing template
#: overrides + a CSS bundle at ``saebooks/static/themes/<name>/app.css``.
ACTIVE_THEMES: frozenset[str] = frozenset({DEFAULT_THEME, CLASSIC_THEME})


class ThemeError(ValueError):
    """Raised when a theme name isn't in :data:`ACTIVE_THEMES`."""


def _normalise(value: str | None) -> str | None:
    """Trim + lowercase, returning ``None`` for empty/whitespace."""

    if value is None:
        return None
    v = value.strip().lower()
    return v or None


def _coerce(value: str | None) -> str | None:
    """Return ``value`` if it's a known active theme, else ``None``."""

    v = _normalise(value)
    if v is None or v not in ACTIVE_THEMES:
        return None
    return v


def resolve_theme(
    settings: Settings,
    *,
    user_preferred: str | None = None,
    db_setting: str | None = None,
) -> str:
    """Resolve the theme for the current context.

    Precedence (first valid wins): ``user_preferred`` > env var
    (``settings.frontend``) > ``db_setting`` > :data:`DEFAULT_THEME`.
    Values not in :data:`ACTIVE_THEMES` are silently ignored so a stale
    row never breaks a page — the startup path uses
    :func:`validate_startup_theme` to catch typos loudly.
    """

    for candidate in (user_preferred, settings.frontend, db_setting):
        coerced = _coerce(candidate)
        if coerced is not None:
            return coerced
    return DEFAULT_THEME


def validate_startup_theme(name: str) -> str:
    """Raise :class:`ThemeError` if ``name`` isn't active.

    Used at ``create_app`` time + in ``/admin/theme`` POSTs so an
    invalid selection can never be persisted.
    """

    v = _normalise(name) or DEFAULT_THEME
    if v not in ACTIVE_THEMES:
        raise ThemeError(
            f"Unknown theme {name!r}. Active themes: "
            f"{sorted(ACTIVE_THEMES)}"
        )
    return v


# ---------------------------------------------------------------- #
# Jinja loader factory
# ---------------------------------------------------------------- #


def build_loader(
    *,
    templates_dir: Path,
    themes_dir: Path,
    active_theme: str,
) -> ChoiceLoader:
    """Build the app-wide Jinja loader chain.

    The active theme's directory is searched **first** so any overridden
    template (e.g. ``dashboard/index.html``) wins over the flat default.
    The flat default is always the fallback, so themes only need to
    override the pages they actually want to change.
    """

    active_theme = validate_startup_theme(active_theme)
    loaders = []
    if active_theme != DEFAULT_THEME:
        theme_path = themes_dir / active_theme
        loaders.append(FileSystemLoader(str(theme_path)))
    loaders.append(FileSystemLoader(str(templates_dir)))
    return ChoiceLoader(loaders)


def build_templates(
    *,
    templates_dir: Path,
    themes_dir: Path,
    active_theme: str,
) -> Jinja2Templates:
    """Construct a :class:`Jinja2Templates` with the ChoiceLoader applied.

    FastAPI's ``Jinja2Templates(directory=...)`` takes a single
    directory, but its underlying env accepts a custom loader — we
    swap it out after construction so the fall-through to defaults is
    automatic.
    """

    t = Jinja2Templates(directory=str(templates_dir))
    t.env.loader = build_loader(
        templates_dir=templates_dir,
        themes_dir=themes_dir,
        active_theme=active_theme,
    )
    return t
