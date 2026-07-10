"""Role-based authorisation — FastAPI deps for role gates.

Pairs with ``saebooks.middleware.auth`` which stamps
``request.state.user`` / ``request.state.role``.

Usage::

    from fastapi import APIRouter, Depends
    from saebooks.services.authz import require_role

    router = APIRouter()

    @router.post(
        "/admin/dangerous",
        dependencies=[Depends(require_role("admin"))],
    )
    async def dangerous() -> ...: ...

``require_role`` is a factory — the returned dep inspects
``request.state.role`` on each request, raising 403 when the role is
absent or below the required rank. 401 is raised when there is no
user at all — that distinguishes "log in please" from "you're logged
in but not allowed".

``current_user`` is the non-gated ready-reference dep for the already-
authenticated request — use it on ``/whoami`` and similar
self-service routes.
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User, UserRole, has_at_least

# NOTE on api.v1.auth.resolve_tenant_id: services/active_company.py
# imports it at MODULE level (safe there — nothing in api/v1/*.py
# imports active_company.py at ITS OWN module level, so there is no
# path back into a partially-initialized active_company module).
# authz.py is different: it is imported at module level by ~15
# routers INSIDE api/v1/__init__.py's eager build_v1_router() loop.
# A module-level `from saebooks.api.v1.auth import resolve_tenant_id`
# here is only safe if something has already fully imported
# saebooks.api.v1.auth before authz.py is first touched — true for
# both real entry points (saebooks.main; tests/conftest.py imports
# saebooks.main.app at module level before any test file runs) but
# NOT guaranteed for an arbitrary script/one-off import order (a
# standalone `import saebooks.services.authz` before anything has
# touched saebooks.api.v1 walks straight into
# "ImportError: cannot import name resolve_tenant_id from partially
# initialized module" — verified empirically in review). Importing
# lazily, inside the one function that needs it, removes the
# fragility entirely at zero runtime cost (it's only ever called once
# per request, already inside an async function).
from saebooks.services import permissions as perm_svc


def _staff_allowlist() -> frozenset[str]:
    """Usernames + emails permitted to use SAE-staff-only routes.

    Read from ``SAE_STAFF_USERNAMES`` (comma-separated). Lower-cased.
    Empty allowlist = everyone is denied (correct fail-closed default).
    """
    raw = os.environ.get("SAE_STAFF_USERNAMES", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def require_staff() -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no user, 403 unless user is in ``SAE_STAFF_USERNAMES``.

    For routes that bypass tenant RLS (raw SQL, cross-tenant tooling).
    A tenant admin must NOT be enough — those routes can read every
    tenant's data, so we gate by an explicit operator allowlist.
    """
    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        allow = _staff_allowlist()
        username = (user.username or "").lower()
        email = (user.email or "").lower()
        if username not in allow and email not in allow:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SAE staff only",
            )
        return user

    return _dep


def current_user(request: Request) -> User | None:
    """Return the user attached by ForwardAuthMiddleware, or ``None``."""
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    assert isinstance(user, User)
    return user


def resolve_actor_role(request: Request) -> str | None:
    """Role string for the F-04 period-lock override gate, HTML-route flavour.

    Prefers ``request.state.role`` (stamped by ForwardAuthMiddleware),
    falls back to the authenticated user's own ``role``, then the
    ``X-Actor-Role`` header (service-to-service escape hatch), else
    ``None`` so the service layer fails closed. The JSON API has its own
    richer resolver (``api/v1/journal_entries._resolve_actor_role``) that
    also honours the static dev bearer; HTML routes never see that token.
    """
    role = getattr(request.state, "role", None)
    if role:
        return str(role)
    user = current_user(request)
    if user is not None and getattr(user, "role", None):
        return str(user.role)
    hdr = request.headers.get("x-actor-role")
    if hdr:
        return hdr.strip()
    return None


def require_user() -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no authenticated user on the request."""
    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        return user

    return _dep


async def no_additional_gate(request: Request) -> None:
    """No-op fallback for ``require_permission_or_role`` — for a route
    that had NO gate beyond the router-level ``require_bearer`` before
    this module existed.

    Deliberately does NOT require ``current_user(request)`` to be
    non-``None`` — ``require_bearer`` itself never populates
    ``request.state.user`` for the static-dev-token path (scripts,
    CLI, MCP, the docker suite's non-JWT callers), and that path was
    always allowed through on these routes. Using ``require_user()``
    here instead would 401 that path below-tier — a real regression
    caught in review, NOT the "byte-identical below-tier" guarantee
    this module promises. See callers in ``api/v1/*.py`` (invoice/
    bill/payment/credit_note/journal post+void, tax_code.manage,
    bank_account.manage, settings.edit, reconciliation.*) for which
    routes this applies to; routes that already had a real gate
    (``_require_admin``, etc.) pass THAT dependency as ``fallback``
    instead, never this one.
    """
    return None


def require_role(
    minimum: str | UserRole,
) -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no user, 403 if the user's role ranks below ``minimum``.

    ``minimum`` can be either the string literal (``"admin"``) or the
    enum member (``UserRole.ADMIN``). Role hierarchy follows
    ``models.user._ROLE_RANK`` — admin > accountant > bookkeeper >
    readonly > client.
    """
    required = minimum.value if isinstance(minimum, UserRole) else str(minimum)

    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        role = getattr(request.state, "role", None) or user.role
        if not has_at_least(role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{required}' required (you have '{role}')",
            )
        return user

    return _dep


async def _resolve_and_cache(request: Request, user: User) -> frozenset[str]:
    """Lazily resolve the request's permission set, cache on state.

    Binds ``app.current_tenant`` on the fresh session before running
    any query — CRITICAL now that ``roles`` and ``role_permissions``
    carry FORCE ROW LEVEL SECURITY (granular_permissions module,
    migrations 0190/0194). Without this, a bare ``AsyncSessionLocal()``
    has no tenant GUC set; under FORCE RLS the ``tenant_isolation``
    policy predicate evaluates NULL and every query returns ZERO rows
    — silently resolving every user's permission set to empty, which
    would lock even an Owner/Admin out of every route this gates. This
    was a real gap caught in review before any router used
    ``require_permission`` — ``user_permissions`` was already FORCE-RLS'd
    (migration 0191) with the same exposure, just never triggered
    because nothing called this path with real DB rows to hide before
    now. Stamping ``session.info["tenant_id"]`` reuses the SAME
    ``after_begin`` listener ``api/v1/deps.py`` registers process-wide
    (any session with a tenant in ``.info`` gets the GUC re-issued on
    every transaction, not just the first) — see that module's
    docstring for why a one-shot ``SET`` at session-open isn't enough.
    """
    cached: frozenset[str] | None = getattr(
        request.state, "permissions", None
    )
    if cached is not None:
        return cached
    from saebooks.api.v1.auth import resolve_tenant_id

    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_id)
        resolved = await perm_svc.resolve_permissions(session, user)
    request.state.permissions = resolved
    return resolved


def require_permission(
    code: str,
) -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no user, 403 if ``code`` isn't in the user's permission set.

    Uses ``services/permissions.resolve_permissions`` which composes
    role grants + per-user overrides. Resolution is cached on
    ``request.state.permissions`` so multiple permission-gated deps
    on the same request only hit the DB once.
    """

    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        perms = await _resolve_and_cache(request, user)
        if code not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission {code!r} required",
            )
        return user

    return _dep


def require_permission_or_role(
    code: str,
    fallback: Callable[[Request], Awaitable[User]],
) -> Callable[[Request], Awaitable[User]]:
    """Dep: fine-grained ``require_permission`` at-tier, ``fallback`` below.

    The granular_permissions module (D2/D3/D4) is tier-gated —
    ``FLAG_GRANULAR_PERMISSIONS`` is Offline+ per CHARTER — so this is
    the enforcement boundary: an entitled tenant gets the finalized
    per-permission matrix (custom roles, per-user overrides, the D1
    bookkeeper/approver split); a below-tier tenant gets EXACTLY
    whatever gate already existed on that route before this module —
    ``fallback`` must be that pre-existing dependency verbatim (e.g.
    ``require_role(UserRole.ACCOUNTANT)`` on a route that already had
    it, or a permissive no-op on a route that had none beyond
    ``require_bearer`` — see the callers in ``api/v1/*.py`` for which
    is which). This is a deliberate, reviewed choice: several
    post/void-class routes (``invoice.post``, ``bill.post``,
    ``payment.post``, ...) carry NO role gate at all today beyond
    bearer auth — a real, pre-existing authz gap, but tightening it
    for every tenant regardless of entitlement would be a user-visible
    behaviour change to every existing (1-2 person shop) install, not
    a change this tier-gated module should make silently. See the
    module's build report for the explicit follow-up flag.

    Never weakens anything — this only ever ADDS a check for entitled
    tenants; the below-tier path is byte-identical to what ran before
    this dependency existed.

    For a route that had NO gate beyond ``require_bearer`` before this
    module, pass ``no_additional_gate`` as ``fallback`` — NOT
    ``require_user()``. ``require_bearer`` never populates
    ``request.state.user`` for the static-dev-token path (scripts,
    CLI, the docker suite), and that path was always allowed through
    on these routes; ``require_user()`` would 401 it below-tier, which
    is a real behaviour change, not the "byte-identical" guarantee
    above. Reserve ``require_user()`` for a route that already
    genuinely required SOME authenticated identity (rare — most of
    this codebase's role gates check ``request.state.user`` when
    present and fall back to a header for the static-token path
    instead, exactly what ``no_additional_gate`` preserves).

    AT-TIER static-bearer carve-out. ``require_permission`` itself
    401s when ``current_user(request)`` is ``None`` — correct for a
    route that has ALWAYS required a real identity, but wrong here:
    the static ``SAEBOOKS_DEV_API_TOKEN`` path never hydrates
    ``request.state.user`` (see ``api/v1/auth.py::require_bearer``'s
    final branch), yet it has always been treated as a superuser/
    ops credential that sails through every role gate in this
    codebase (``_require_admin`` et al. check an ``X-Admin`` header
    for exactly this reason). Taking the fine-grained branch
    unconditionally at-tier would 401 that credential the instant a
    tenant enables the feature — a real regression, and the mirror
    image of the ``no_additional_gate`` fix above. So: only take the
    ``require_permission`` branch when BOTH the feature is on AND a
    user is actually hydrated on the request; otherwise fall through
    to ``fallback`` exactly as below-tier. This does not weaken
    anything in production — real end users always arrive via JWT
    (hydrated by ``_stamp_user_from_sub``) and machine automation via
    the ``saebk_`` API-token path (hydrated at ``auth.py:471``), so
    both still get full fine-grained enforcement at-tier. Only the
    static/dev/ops credential — which every other gate in this
    codebase already treats as pre-authorised — skips to ``fallback``,
    keeping its behaviour tier-invariant instead of flipping to a 401
    the moment a tenant upgrades.
    """
    from saebooks.services.features import (
        FLAG_GRANULAR_PERMISSIONS,
        feature_enabled_for_request,
    )

    permission_dep = require_permission(code)

    async def _dep(request: Request) -> User:
        if (
            current_user(request) is not None
            and feature_enabled_for_request(FLAG_GRANULAR_PERMISSIONS, request)
        ):
            return await permission_dep(request)
        return await fallback(request)

    return _dep


async def require_permission_or_role_inline(
    code: str,
    fallback: Callable[[Request], Awaitable[User]],
    request: Request,
) -> None:
    """Non-dependency-injected form of ``require_permission_or_role``.

    Same semantics, called directly instead of via FastAPI's dependency
    graph — for a route whose gate can't be decided statically at
    ``Depends()`` time (e.g. ``tax_returns.py``'s single ``/lodge``
    endpoint, which dispatches to bas.lodge / tax_return.lodge /
    payroll.run / tpar.finalise / super_lodgement.finalise depending
    on the return's ``return_type``, only known after a DB lookup
    inside the handler). Mirrors ``services.features.
    require_feature_inline``'s relationship to ``require_feature``.

    Same static-bearer carve-out as ``require_permission_or_role``:
    only takes the fine-grained branch when a user is actually
    hydrated on the request — see that function's docstring for why.
    """
    from saebooks.services.features import (
        FLAG_GRANULAR_PERMISSIONS,
        feature_enabled_for_request,
    )

    if (
        current_user(request) is not None
        and feature_enabled_for_request(FLAG_GRANULAR_PERMISSIONS, request)
    ):
        await require_permission(code)(request)
    else:
        await fallback(request)


async def permissions_for(request: Request) -> frozenset[str]:
    """Reader dep for templates + debug routes — never 401/403.

    Returns an empty frozenset when no user is on the request (anonymous
    / open path). Templates that want to hide a button unless the user
    has a permission should read the value off ``request.state.permissions``
    directly (populated by this dep on first call).
    """
    user = current_user(request)
    if user is None:
        return frozenset()
    return await _resolve_and_cache(request, user)
