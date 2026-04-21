"""Feature-flag / licence-gate module.

Per ``CHARTER.md §6.2`` the SAE Books codebase is a single AGPL tree that
supports two editions:

* **Community** (free, AGPL) — complete single-company bookkeeping.
* **Enterprise** (same code, commercial licence) — ships with features
  like bank feeds and ABR lookup exposed in the UI.

All Enterprise features live in this repo, but UI routes that surface
them must be *runtime-gated* so a Community build doesn't silently ship
a feature we intend to charge for. This module is the gate.

The source of truth for the active edition is ``settings.edition``
(configured via ``SAEBOOKS_EDITION``) — see ``saebooks/config.py``.
Licence-key JWTs or per-company overrides may come later; the public
API here (``is_enabled``, ``require_feature``) is stable and will get
new backends plugged in beneath it.

Usage::

    from fastapi import APIRouter, Depends
    from saebooks.services.features import (
        FLAG_BANK_FEEDS, require_feature,
    )

    router = APIRouter(prefix="/admin/bank-feeds")

    @router.get("/", dependencies=[Depends(require_feature(FLAG_BANK_FEEDS))])
    async def index() -> ...: ...
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, status

from saebooks.config import Settings
from saebooks.config import settings as _default_settings

# ---------------------------------------------------------------------- #
# Flag identifiers                                                       #
# ---------------------------------------------------------------------- #

FLAG_BANK_FEEDS = "bank_feeds"
FLAG_ABR_LOOKUP = "abr_lookup"
FLAG_LEI_LOOKUP = "lei_lookup"
FLAG_MULTI_COMPANY = "multi_company"
FLAG_EXTENDED_AUDIT_MODES = "extended_audit_modes"

ALL_FLAGS: tuple[str, ...] = (
    FLAG_BANK_FEEDS,
    FLAG_ABR_LOOKUP,
    FLAG_LEI_LOOKUP,
    FLAG_MULTI_COMPANY,
    FLAG_EXTENDED_AUDIT_MODES,
)

# Every flag defined above is currently Enterprise-only. Community gets
# nothing from this set; Enterprise gets everything. When we introduce a
# finer-grained edition matrix (e.g. per-feature add-ons), swap this for
# a ``dict[str, frozenset[str]]`` mapping edition -> enabled flags.
_ENTERPRISE_FLAGS: frozenset[str] = frozenset(ALL_FLAGS)


# ---------------------------------------------------------------------- #
# Public API                                                             #
# ---------------------------------------------------------------------- #


def is_enabled(flag: str, *, settings: Settings | None = None) -> bool:
    """Return ``True`` when ``flag`` is active under the given settings.

    ``settings`` defaults to the module-level singleton; pass an explicit
    ``Settings`` for tests that want to exercise alternate editions
    without monkey-patching.

    Unknown flags raise ``ValueError`` — typoed flag names should fail
    loud rather than silently return ``False`` (which would hide an
    Enterprise feature in an Enterprise build).
    """
    if flag not in _ENTERPRISE_FLAGS:
        raise ValueError(f"Unknown feature flag: {flag!r}")
    effective = settings if settings is not None else _default_settings
    return effective.edition == "enterprise"


def active_flags(*, settings: Settings | None = None) -> dict[str, bool]:
    """Return ``{flag_name: enabled}`` for every known flag.

    Used by ``/admin/license`` to render the flag matrix.
    """
    return {flag: is_enabled(flag, settings=settings) for flag in ALL_FLAGS}


def require_feature(flag: str) -> Callable[[], Awaitable[None]]:
    """FastAPI dependency factory: 404 when ``flag`` is disabled.

    Attach via ``Depends(require_feature(FLAG_X))`` or on a router via
    ``dependencies=[Depends(require_feature(FLAG_X))]``.

    Returns 404 (not 403) so a Community build doesn't advertise the
    existence of Enterprise routes — they simply aren't part of the
    build, which matches how the feature looks from the outside.
    """
    # Validate the flag name at decoration time so a typo in a router
    # module fails at import, not on first request.
    if flag not in _ENTERPRISE_FLAGS:
        raise ValueError(f"Unknown feature flag: {flag!r}")

    async def _dep() -> None:
        if not is_enabled(flag):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return _dep
