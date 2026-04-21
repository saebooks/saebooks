"""Shared Jinja2Templates factory.

Every router historically built its own ``Jinja2Templates`` pointing at
``saebooks/templates/``. Batch QQ consolidates that into one module so
the loader chain (``ChoiceLoader([active_theme, default])``) can be
installed once and reused everywhere — no theme-aware code needs to
live inside routers.

We also expose a Jinja global ``theme_for_request(request)`` so base
templates can switch the per-user CSS bundle without going through
any middleware. The *template tree* is global (set at startup from
``settings.frontend``); the *CSS bundle* is per-request (wins from
``request.state.user.preferred_theme``).

The active theme is read at import time from ``settings.frontend``.
An admin-triggered theme change via ``/admin/theme`` persists to the
``settings`` table but does not reload the loader chain in-process —
restart the app for the template tree to pick up the new theme.
Per-user CSS-bundle changes take effect immediately (no restart).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from saebooks.config import settings
from saebooks.services.theme import (
    DEFAULT_THEME,
    build_templates,
    resolve_theme,
)

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"
THEMES_DIR: Path = TEMPLATES_DIR / "themes"

# The one and only Jinja2Templates instance in the app. Routers import
# this directly; they do not build their own. The ChoiceLoader inside
# it searches ``templates/themes/<active>/`` first, then ``templates/``
# as the fallback — so a theme only overrides the pages it actually
# changes and every other page renders the default.
templates: Jinja2Templates = build_templates(
    templates_dir=TEMPLATES_DIR,
    themes_dir=THEMES_DIR,
    active_theme=settings.frontend or DEFAULT_THEME,
)


def _theme_for_request(request: Request) -> str:
    """Return the effective theme name for this request.

    Used as a Jinja global so ``base.html`` can pick the CSS bundle:
    ``<link href="/static/themes/{{ theme_for_request(request) }}/app.css">``.

    Precedence is delegated to :func:`saebooks.services.theme.resolve_theme`
    which reads user > env > (DB setting — checked lazily at app boot,
    not per request). When ``request.state.user`` is unset (anonymous
    health/metrics/static), we still return a valid theme.
    """

    user = getattr(request.state, "user", None)
    user_pref = getattr(user, "preferred_theme", None) if user else None
    return resolve_theme(settings, user_preferred=user_pref)


# Register the global so every template can call it.
templates.env.globals["theme_for_request"] = _theme_for_request
