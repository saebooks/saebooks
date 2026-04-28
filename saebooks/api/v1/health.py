"""Unauthenticated health + version endpoints for the v1 API surface.

Why these live in ``/api/v1/`` alongside the bearer-gated endpoints:

* Downstream uptime probes (Cloudflare, self-hosted Uptime Kuma, the
  agent-core watchdog) should hit a single stable URL.  The legacy
  top-level ``/healthz`` route lives inside ``saebooks.routers.health``
  and is slated for removal once Phase 3 tears the legacy HTML router
  layer out.  Moving the canonical liveness path to ``/api/v1/healthz``
  now means probes can be migrated before the legacy router is deleted.
* ``/api/v1/version`` gives downstream clients (saebooks-web,
  saebooks-desktop, saebooks-tools) a bearer-free way to introspect the
  API's edition + version so they can show a "connected to SAE Books
  vX (community)" banner without burning a bearer call.

Both routes are mounted WITHOUT the ``require_bearer`` dependency that
every other v1 router uses — they are intentionally open.  They are
still inside the ``/api/`` prefix which the ForwardAuthMiddleware
treats as open (see ``saebooks/middleware/auth.py``
``OPEN_PATH_PREFIXES``) so no Authentik user upsert is triggered.

Nothing sensitive is returned.  ``edition`` is derived from
``SAEBOOKS_EDITION`` (defaults to ``community``) and is already
displayed on every HTML page.  ``version`` is the static
``pyproject.toml`` project version read via
``importlib.metadata.version``.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as pkg_version

from fastapi import APIRouter

from saebooks.config import settings

router = APIRouter(tags=["health"])


def _package_version() -> str:
    """Best-effort package version lookup.

    Falls back to the FastAPI app's version string (``0.0.1``) if the
    package isn't installed into site-packages (e.g. tests running from
    a source checkout without an editable install).
    """
    try:
        return pkg_version("saebooks")
    except PackageNotFoundError:
        return "0.0.1"


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — always 200 while the process is up.

    No DB round-trip on purpose: a failed DB should still let the
    platform layer see the app is running so it can report a
    ``degraded`` state from a separate ``/readyz`` hit (not yet
    implemented).
    """
    return {"status": "ok", "edition": settings.edition}


@router.get("/version")
async def api_version() -> dict[str, str]:
    """Return API edition + package version.

    Deliberately open (no bearer) so downstream clients can render a
    "connected to" banner before the user has signed in.
    """
    return {
        "edition": settings.edition,
        "version": _package_version(),
        "api": "v1",
    }


@router.get("/license")
async def api_license() -> dict[str, object]:
    """Return the active edition and per-flag matrix.

    Deliberately open (no bearer) — the edition and enabled flags are
    non-sensitive public metadata already shown on the /admin/license
    HTML page. The web frontend calls this to conditionally render
    multi-company UI elements without burning an auth'd call.
    """
    from saebooks.services.features import ALL_FLAGS, TIER_ORDER, active_flags

    return {
        "edition": settings.edition,
        "flags": active_flags(),
        "all_flags": list(ALL_FLAGS),
        "tier_order": list(TIER_ORDER),
    }
