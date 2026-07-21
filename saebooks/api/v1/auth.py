"""Bearer-token auth for the v1 API — Phase 0 dev wiring.

Reads the token from the ``SAEBOOKS_DEV_API_TOKEN`` env var. If the
var is unset, we generate a per-process random token at import time so
running the server without explicit config gives a secure default
(rather than silently accepting any bearer). The random value is
logged at INFO so a developer running the POC script can grab it from
the server log.

Multi-tenant wiring (P0 cross-tenant leak fix)
----------------------------------------------
After the bearer is verified, ``require_bearer`` decodes the JWT (when
present) and stamps the claims onto ``request.state.jwt_claims``. The
shared session dependency (``saebooks.api.v1.deps.get_session``) reads
those claims and issues ``SET LOCAL app.current_tenant`` on the one
session it yields per request, so every query the handler runs is
bound by the ``tenant_isolation`` RLS policy.

``resolve_tenant_id`` reads the JWT claim from ``request.state``
(falling back to the static dev env var only when ``SAEBOOKS_ENV=dev``)
so handlers that still need the raw tenant id for explicit filtering
get the request's tenant — never the historical hard-coded default.

Password-version invalidation (0077)
------------------------------------
JWTs minted by ``services.jwt_tokens.make_access_token`` carry a
``pwv`` claim equal to ``user.password_version`` at mint time. When
the user resets their password we bump the column, so every
previously-issued token fails the ``pwv == user.password_version``
check on the next request. Missing claim (legacy tokens or static
dev bearer) is treated as ``pwv = 0``, which matches the column
default — this keeps backward-compat on rolling deploys.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, Request, status

logger = logging.getLogger("saebooks.api.auth")

_ENV_VAR = "SAEBOOKS_DEV_API_TOKEN"
_TENANT_ENV_VAR = "SAEBOOKS_DEV_TENANT_ID"
_DEV_ENV_GUARD = "SAEBOOKS_ENV"

# Default tenant UUID — matches the seed row in migration 0040_tenants.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _resolve_token() -> str:
    token = os.environ.get(_ENV_VAR, "").strip()
    if token:
        return token
    # Generate once per process. NEVER print the token VALUE outside an
    # explicit dev/test environment: the one-click server (SAEBOOKS_ENV
    # unset / production) runs with a user-facing console, and a cleartext
    # bearer on stdout is a secret-to-stdout leak — scary and quotable. On a
    # real dev box (SAEBOOKS_ENV=dev/test) the value is a convenience so the
    # developer can grab it; there stdout is the developer's own terminal.
    generated = secrets.token_urlsafe(32)
    _env = os.environ.get(_DEV_ENV_GUARD, "").strip().lower()
    if _env in ("dev", "development", "test"):
        logger.info(
            "%s not set; using ephemeral dev token (pass as 'Authorization: Bearer %s')",
            _ENV_VAR,
            generated,
        )
    else:
        logger.info(
            "%s not set; generated a random ephemeral API token for this "
            "process (value suppressed). Set %s to a fixed value to pin it "
            "across restarts and to authenticate API clients.",
            _ENV_VAR,
            _ENV_VAR,
        )
    os.environ[_ENV_VAR] = generated
    return generated


_TOKEN = _resolve_token()


def current_token() -> str:
    """Return the process-wide expected bearer token (testing hook)."""
    return os.environ.get(_ENV_VAR, _TOKEN)


async def _stamp_user_from_sub(request: Request, claims: dict[str, object]) -> None:
    """Resolve JWT ``sub`` to a User row and stamp request.state.

    Best-effort: any failure (no sub, malformed sub, missing user, DB
    hiccup, archived user) leaves ``request.state.user`` and
    ``request.state.role`` as None. The downstream admin gates already
    fall back to the X-Admin header in that case (used by the static
    dev-bearer path in tests/scripts).

    Also enforces the ``pwv`` (password-version) claim — a JWT whose
    pwv doesn't match the live user row is rejected with 401, so a
    password reset invalidates every issued token globally.
    """
    sub = claims.get("sub")
    if not sub:
        return
    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        return

    # Local imports to avoid circulars at module load time.
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.user import User

    # Bind app.current_tenant from the JWT claim BEFORE the SELECT, otherwise
    # FORCE-RLS on ``users`` silently drops every row and the caller looks
    # like a token with no live user — admin gates then 403 because they
    # fall back to the X-Admin header path. Mirrors the pattern in
    # ``api/v1/login.py::_get_user`` and the 5c9b3c1 /auth/me fix.
    tenant_claim = claims.get("tenant_id")
    try:
        async with AsyncSessionLocal() as session:
            if tenant_claim:
                session.info["tenant_id"] = str(tenant_claim)
            user = await session.get(User, user_id)
    except Exception as exc:  # defensive — DB hiccup shouldn't 500
        logger.warning("require_bearer user lookup failed for sub=%s: %s", sub, exc)
        return
    if user is None or user.archived_at is not None:
        return

    # pwv enforcement — token's pwv must match the current row.
    # Missing claim treated as 0; default column value is 0; both
    # match for legacy tokens until the user resets their password.
    token_pwv = int(claims.get("pwv", 0) or 0)
    user_pwv = int(user.password_version or 0)
    if token_pwv != user_pwv:
        logger.info(
            "require_bearer: pwv mismatch sub=%s token_pwv=%d user_pwv=%d",
            sub,
            token_pwv,
            user_pwv,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalidated by password change — please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.user = user
    request.state.role = user.role


async def _enforce_principal_grant(
    request: Request, claims: dict[str, object]
) -> None:
    """Re-verify a principal-type bearer's grant on the SHARED auth path.

    Cross-tenant accountant ("principal") sessions are minted by
    ``/api/v1/principal/act-as`` as JWTs signed with the SAME secret as user
    JWTs, but carrying ``typ="principal"`` + ``psub`` (principal id) instead of
    ``sub`` (user id). ``decode_access_token`` validates the signature/expiry of
    *any* such token — it does not key on ``typ`` — so a principal token reaches
    ``require_bearer`` and would otherwise have its ``tenant_id`` claim stamped
    onto ``request.state.jwt_claims`` and bound to ``app.current_tenant`` by
    ``get_session`` with NO grant re-check. That made the entire user API
    (companies, contacts, invoices, …) reachable by a bound principal token, and
    left a revoked grant exploitable for the token's whole 1h TTL on the user
    router (the headline A2 hole).

    This function closes that hole by enforcing the grant on the shared path,
    PER REQUEST, BEFORE any tenant is bound:

    * Detects a principal-type token (``typ == "principal"`` OR a ``psub`` claim
      is present). Normal user tokens (``sub`` + ``tenant_id``, no ``typ``/
      ``psub``) never enter this branch — their behaviour is byte-for-byte
      unchanged.
    * An UNBOUND principal token (no ``tenant_id``) has no business on a user
      data router → 403. (It can still drive ``/principal/tenants`` +
      ``/principal/act-as`` via the principal router, which uses its own bearer
      dependency.)
    * A bound principal token is verified via ``resolve_grant_role`` — the SAME
      SECURITY DEFINER predicate ``/act-as`` and ``get_principal_tenant_session``
      use. No ACTIVE grant for (psub, tenant_id) → 403, and NO binding (we raise
      before ``require_bearer`` stamps the claims). Because this runs on every
      request, a revoked grant takes effect IMMEDIATELY on the user router too.

    There is no BYPASSRLS path: ``resolve_grant_role`` is parameterised by
    (principal, tenant) and is independent of ``app.current_tenant``, so we can
    call it on a fresh session before any tenant GUC is set — exactly as the
    principal router does. Fail closed: any error resolving the grant denies.
    """
    from saebooks.services.principal_session import PRINCIPAL_TOKEN_TYPE

    is_principal = (
        claims.get("typ") == PRINCIPAL_TOKEN_TYPE or claims.get("psub") is not None
    )
    if not is_principal:
        return  # normal user token — leave the existing path untouched.

    psub = claims.get("psub")
    if not psub:
        # typ=principal but no psub is a malformed principal token.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="principal token missing psub",
        )
    try:
        principal_id = uuid.UUID(str(psub))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="principal token psub is not a valid UUID",
        ) from exc

    tenant_claim = claims.get("tenant_id")
    if not tenant_claim:
        # Unbound principal login token — not valid on a user data router.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "unbound principal session; call /api/v1/principal/act-as "
                "before using the user API"
            ),
        )
    try:
        tenant_id = uuid.UUID(str(tenant_claim))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="principal token tenant_id is not a valid UUID",
        ) from exc

    # Re-verify the live grant under the app role, before binding anything.
    # resolve_grant_role -> principal_grant_role (SECURITY DEFINER) does NOT
    # depend on app.current_tenant; identical call to the principal router.
    from saebooks.db import AsyncSessionLocal
    from saebooks.services.principal import resolve_grant_role

    try:
        async with AsyncSessionLocal() as session:
            role = await resolve_grant_role(session, principal_id, tenant_id)
    except HTTPException:
        raise
    except Exception as exc:  # fail closed — never proceed on a lookup error.
        logger.warning(
            "principal grant check failed for psub=%s tenant=%s: %s",
            principal_id,
            tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="could not verify principal grant",
        ) from exc

    if role is None:
        logger.info(
            "principal grant denied on user path: psub=%s tenant=%s",
            principal_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no active grant for the requested tenant",
        )

    # Verified. Stamp the principal context for downstream audit attribution.
    # We do NOT hydrate request.state.user/role — a principal token confers no
    # user identity, so admin gates keep denying it exactly as before.
    request.state.principal_role = role
    request.state.principal_id = principal_id


def _is_dev_env() -> bool:
    """True when the process is in a dev/test environment.

    Used to guard the env-var tenant override so a misconfigured prod
    container can never silently fall back to the historical default
    tenant. ``pytest`` always sets ``SAEBOOKS_ENV=test``? No — it
    doesn't, by default. We accept any of ``dev``, ``test``,
    ``development``, ``testing`` (case-insensitive) for the override.
    """
    raw = os.environ.get(_DEV_ENV_GUARD, "").strip().lower()
    return raw in {"dev", "test", "development", "testing"}


def resolve_tenant_id(request: Request | None = None) -> uuid.UUID:
    """Resolve the tenant UUID for the current request.

    Preference order:

    1. ``request.state.jwt_claims["tenant_id"]`` — set by
       ``require_bearer`` after decoding the JWT.
    2. ``SAEBOOKS_DEV_TENANT_ID`` env var — only honoured when
       ``SAEBOOKS_ENV`` indicates a dev/test environment, so prod
       can't silently leak into the default tenant when the JWT is
       missing.
    3. Hard-coded default UUID — only as a final fallback in dev/test.

    Raises ``HTTPException(401)`` outside dev/test if neither the JWT
    nor a request-state claim is present.
    """
    # FLAG_TENANT_SWITCHER override — when the developer-tier flag is active
    # AND the caller is admin AND the X-Active-Tenant header is set, use that
    # tenant id instead of the JWT claim. Lets the operator switch tenants in
    # the UI without re-authenticating. Gated triply so non-developer
    # instances ignore the header entirely.
    if request is not None:
        x_tenant = request.headers.get("x-active-tenant", "").strip()
        if x_tenant:
            try:
                from saebooks.config import settings as _s
                from saebooks.models.user import UserRole, has_at_least
                from saebooks.services.features import (
                    FLAG_TENANT_SWITCHER,
                )
                from saebooks.services.features import (
                    is_enabled as _flag_enabled,
                )
                if _flag_enabled(FLAG_TENANT_SWITCHER, settings=_s):
                    role = getattr(request.state, "role", None)
                    if not role:
                        u = getattr(request.state, "user", None)
                        role = getattr(u, "role", None) if u else None
                    if role and has_at_least(role, UserRole.ADMIN.value):
                        try:
                            return uuid.UUID(x_tenant)
                        except ValueError:
                            pass
            except Exception:
                pass

    if request is not None:
        claims = getattr(request.state, "jwt_claims", None)
        if claims and "tenant_id" in claims:
            try:
                return uuid.UUID(str(claims["tenant_id"]))
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="JWT tenant_id is not a valid UUID",
                ) from exc

    if _is_dev_env():
        raw = os.environ.get(_TENANT_ENV_VAR, "").strip()
        if raw:
            try:
                return uuid.UUID(raw)
            except ValueError:
                logger.warning(
                    "Invalid %s value '%s'; using default tenant",
                    _TENANT_ENV_VAR,
                    raw,
                )
        return DEFAULT_TENANT_ID

    # Production with no JWT claim — refuse to guess the tenant.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No tenant on request — JWT missing tenant_id claim",
    )


async def require_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: enforce Bearer auth on every v1 endpoint.

    Accepts either a JWT issued by POST /auth/login or the static
    SAEBOOKS_DEV_API_TOKEN (backward-compat for scripts and tests).

    On success, when the bearer is a JWT, stamps the decoded claims
    onto ``request.state.jwt_claims`` so ``get_session`` /
    ``resolve_tenant_id`` can read the tenant. Additionally, when the
    JWT carries a ``sub`` claim that resolves to a live User row,
    stamps ``request.state.user`` and ``request.state.role`` so admin
    gates (``users._require_admin``, ``hard_delete_admin_gate``) can
    enforce role server-side instead of trusting a self-asserted
    ``X-Admin: true`` header. This closes the JSON-API admin-elevation
    hole — a bookkeeper JWT cannot bypass the gate by adding the
    header.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(None, 1)[1].strip()

    # Accept valid JWTs issued by /auth/login.
    from saebooks.services.jwt_tokens import JWTError, decode_access_token
    try:
        claims = decode_access_token(presented)
        # PRINCIPAL-TYPE TOKEN GATE (shared path). A principal session token is
        # a validly-signed JWT (same secret), so it decodes here too. Before we
        # stamp any claims or let get_session bind app.current_tenant, re-verify
        # the principal's ACTIVE grant for the token's tenant — per request, so
        # a revoked grant is enforced IMMEDIATELY on the user router as well
        # (closes A1/A2). Raises 403 with NO binding when the grant is absent.
        # No-op for normal user tokens (sub + tenant_id, no typ/psub), so the
        # existing user-auth path is byte-for-byte unchanged.
        await _enforce_principal_grant(request, claims)
        # Stamp the claims onto request.state so the session dep and
        # downstream handlers can see the tenant. Old code decoded and
        # discarded the claims — this was bug #3 in the leak diagnosis.
        request.state.jwt_claims = claims
        await _stamp_user_from_sub(request, claims)
        return presented
    except JWTError:
        pass

    # Machine API token branch — ``saebk_<64hex>``. Cleanly separated
    # from the JWT path so an invalid JWT doesn't accidentally match
    # the prefix and skip ahead. Used by the CLI, MCP server, and any
    # third-party automation. See ``services/api_tokens.py``.
    from saebooks.services.api_tokens import (
        TOKEN_PREFIX_HEADER,
        TokenVerifyError,
    )
    from saebooks.services.api_tokens import (
        verify as verify_api_token,
    )
    if presented.startswith(TOKEN_PREFIX_HEADER):
        # Pre-auth lookup: we don't yet know the tenant — we're resolving it
        # FROM the presented token. api_tokens is FORCE-RLS, so under the
        # NOBYPASSRLS saebooks_app role with no app.current_tenant set this
        # SELECT returns zero rows and every API token 401s. Use the owner
        # role (LoginSessionLocal), exactly like the JWT login path does for
        # the users table. The tenant-scoped session is established afterwards
        # from request.state.jwt_claims stamped below.
        from saebooks.db import LoginSessionLocal
        try:
            async with LoginSessionLocal() as session:
                token_row = await verify_api_token(session, presented)
                await session.commit()
        except TokenVerifyError as exc:
            logger.info("api token rejected: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        # ---- Scope enforcement (A2) ---------------------------------
        # API-token auth ONLY. Interactive JWT / static-dev-bearer paths
        # return BEFORE reaching here, so their role-based authz is
        # unchanged. A token whose scopes are empty/None or a full-access
        # marker ("*" / "full" / both "read"+"write") keeps full access
        # exactly as before this layer existed -- so every existing live
        # token (issued with the default scopes=[]) is unaffected. Only an
        # explicitly restrictive set (e.g. ["read"]) is limited: safe
        # methods need "read", mutating methods need "write".
        from saebooks.services.scopes import (
            method_requires_scope,
            token_allows,
        )
        if not token_allows(getattr(token_row, "scopes", None), request.method):
            required = method_requires_scope(request.method)
            logger.info(
                "api token scope deny: prefix=%s method=%s scopes=%s",
                getattr(token_row, "token_prefix", "?"),
                request.method,
                getattr(token_row, "scopes", None),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "API token scope does not permit this operation "
                    f"({request.method} requires the '{required}' scope)"
                ),
            )

        # Stamp request.state so downstream handlers see the same
        # shape as the JWT path: jwt_claims (for tenant resolution),
        # user (for role gates), role (string), username.
        request.state.jwt_claims = {
            "sub": str(token_row.user_id),
            "tenant_id": str(token_row.tenant_id),
            "company_id": str(token_row.company_id),
            "api_token": True,
        }
        request.state.user = token_row.user
        request.state.role = getattr(token_row.user, "role", None)
        request.state.username = getattr(token_row.user, "username", None)
        return presented

    # Fall back to static dev token (scripts, tests, direct API access).
    expected = current_token()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Static-bearer path — no JWT claims. In dev/test the
    # SAEBOOKS_DEV_TENANT_ID env var (or hard-coded default) provides
    # the tenant. We synthesise a minimal claims dict here so the
    # session dep doesn't have a special case.
    if _is_dev_env():
        request.state.jwt_claims = {"tenant_id": str(resolve_tenant_id(None))}
    return presented


BearerDep = Depends(require_bearer)


async def require_email_verified(request: Request) -> None:
    """Dep stacked on top of ``require_bearer`` for routes that must
    only run for users who have proved control of their email.

    Static dev-bearer (no ``sub`` claim) bypasses — that path is
    used by tests and scripts where there is no real user.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        # Static-bearer path or a /auth/me-style call before the user
        # is hydrated. Don't gate; the JWT path will hydrate user.
        return
    if user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )
