"""Principal (accountant / bank) authentication + act-as endpoints.

Mounted at ``/api/v1/principal``. This router is ENTIRELY SEPARATE from the
user auth path: it has its own bearer dependency (``require_principal_bearer``)
and its own token type (``services.principal_session``). Nothing here changes
the behaviour of the existing single-tenant user login / enforcement path.

Endpoints
---------
Unauthenticated (entry points — like /auth/login they must mount before any
router-level bearer dependency):

* POST ``/principal/auth/webauthn/authenticate/begin``  — request options
* POST ``/principal/auth/webauthn/authenticate/finish`` — verify assertion,
  mint an UNBOUND principal session. ``principal_id`` is derived from the
  assertion (see ``services.principal_webauthn``), never from the client.

Authenticated principal session (``require_principal_bearer``):

* POST ``/principal/auth/webauthn/register/begin``      — add a key
* POST ``/principal/auth/webauthn/register/finish``     — verify + store
* GET  ``/principal/tenants``                            — list actable tenants
* POST ``/principal/act-as``                             — bind a target tenant
  after verifying an active grant; mints a TENANT-BOUND principal session.

The critical invariant (login)
------------------------------
``authenticate/finish`` calls ``principal_webauthn.complete_authentication``,
which returns a principal id resolved + signature-verified from the credential
the assertion was signed with. The endpoint passes THAT id to the token mint.
There is no request field for a principal id at login. A client cannot claim
an identity; it can only present a key.

The critical invariant (act-as)
-------------------------------
``act-as`` takes the principal id from the AUTHENTICATED session
(``require_principal_bearer``), and a target ``tenant_id`` from the body. It
calls ``resolve_grant_role`` (the SECURITY DEFINER predicate) under the app
role; only if an active grant exists does it mint a tenant-bound token. No
grant -> 403, no token. The bound token's queries then run under the same
FORCE-RLS as a native user — there is no BYPASSRLS data path.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.services import principal as principal_svc
from saebooks.services import principal_webauthn as pw
from saebooks.services.principal_session import (
    PrincipalTokenError,
    decode_principal_token,
    make_principal_token,
)

logger = logging.getLogger("saebooks.api.v1.principal_auth")

# Unauthenticated entry points (login). Mounted first, no router dependency.
auth_router = APIRouter(prefix="/principal", tags=["principal-auth"])

# Authenticated principal endpoints. Router-level dependency enforces a valid
# principal session on every route.
router = APIRouter(prefix="/principal", tags=["principal"])


# --------------------------------------------------------------------------- #
# Dependency: require an authenticated principal session.
# --------------------------------------------------------------------------- #


class PrincipalContext:
    """The authenticated principal, derived from a verified principal token."""

    def __init__(
        self,
        principal_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID | None,
        role: str | None,
    ) -> None:
        self.principal_id = principal_id
        self.tenant_id = tenant_id
        self.role = role


async def require_principal_bearer(request: Request) -> PrincipalContext:
    """Authenticate a principal session from the Authorization header.

    Rejects (401) anything that is not a valid PRINCIPAL token — including a
    perfectly valid *user* JWT, which lacks ``typ="principal"``. This is the
    guard that keeps the user and principal auth surfaces disjoint.
    """
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(None, 1)[1].strip()
    try:
        claims = decode_principal_token(presented)
    except PrincipalTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid principal session",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    principal_id = uuid.UUID(str(claims["psub"]))
    tenant_id = (
        uuid.UUID(str(claims["tenant_id"])) if claims.get("tenant_id") else None
    )
    ctx = PrincipalContext(
        principal_id, tenant_id=tenant_id, role=claims.get("role")
    )
    # Stamp for any downstream use (audit etc.).
    request.state.principal = ctx
    return ctx


async def require_bound_principal(
    ctx: PrincipalContext = Depends(require_principal_bearer),
) -> PrincipalContext:
    """Require a TENANT-BOUND principal session (an act-as token).

    Rejects an unbound login token (403) — the caller must act-as first.
    """
    if ctx.tenant_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "principal session is not bound to a tenant; call /principal/act-as",
        )
    return ctx


async def get_principal_tenant_session(
    ctx: PrincipalContext = Depends(require_bound_principal),
) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the principal's act-as tenant.

    Re-verifies the active grant on EVERY use (revocation takes effect on the
    next request, not just the next login) and then binds
    ``app.current_tenant`` via the SAME ``session.info`` + ``after_begin``
    mechanism a native user's ``deps.get_session`` uses — so every query runs
    under the IDENTICAL FORCE-RLS path. There is no BYPASSRLS data path.

    Re-verification under the app role: ``resolve_grant_role`` calls the
    SECURITY DEFINER predicate; if the grant was revoked since the token was
    minted, it returns None and we 403 before binding anything.
    """
    async with AsyncSessionLocal() as session:
        role = await principal_svc.resolve_grant_role(
            session, ctx.principal_id, ctx.tenant_id
        )
        if role is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "grant revoked or not active for the requested tenant",
            )
        # Bind exactly like deps.get_session (info + after_begin listener).
        session.info["tenant_id"] = str(ctx.tenant_id)
        yield session


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class WebauthnBeginResponse(BaseModel):
    publicKey: dict


class AuthenticateFinishRequest(BaseModel):
    # NOTE: there is deliberately NO principal_id field here. The principal is
    # identified by the credential in the assertion, server-side.
    credential: dict


class PrincipalSessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    principal_id: uuid.UUID
    # Present only on a tenant-bound (act-as) token.
    tenant_id: uuid.UUID | None = None
    role: str | None = None


class RegisterFinishRequest(BaseModel):
    credential: dict
    friendly_name: str = Field(default="Security key", max_length=64)


class RegisterFinishResponse(BaseModel):
    credential_id: str
    friendly_name: str


class ActableTenantOut(BaseModel):
    tenant_id: uuid.UUID
    role: str
    grant_id: uuid.UUID


class ActAsRequest(BaseModel):
    tenant_id: uuid.UUID


# --------------------------------------------------------------------------- #
# Login — unauthenticated entry points.
# --------------------------------------------------------------------------- #


def _pw_error_to_http(exc: pw.PrincipalWebauthnError) -> HTTPException:
    if isinstance(exc, pw.PrincipalWebauthnNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, pw.PrincipalWebauthnChallengeInvalid):
        return HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_challenge")
    if isinstance(exc, pw.PrincipalCredentialNotFound):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "credential_not_found")
    if isinstance(exc, pw.PrincipalAssertionInvalid):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "verification_failed")
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@auth_router.post(
    "/auth/webauthn/authenticate/begin", response_model=WebauthnBeginResponse
)
async def authenticate_begin() -> WebauthnBeginResponse:
    try:
        return WebauthnBeginResponse(**await pw.begin_authentication())
    except pw.PrincipalWebauthnError as exc:
        raise _pw_error_to_http(exc) from exc


@auth_router.post(
    "/auth/webauthn/authenticate/finish",
    response_model=PrincipalSessionResponse,
)
async def authenticate_finish(
    body: AuthenticateFinishRequest,
) -> PrincipalSessionResponse:
    """Verify the assertion and mint an UNBOUND principal session.

    ``principal_id`` is the value returned by ``complete_authentication`` —
    derived from the verified assertion. It is the ONLY source of the id; the
    request carries no principal identifier.
    """
    try:
        principal_id = await pw.complete_authentication(body.credential)
    except pw.PrincipalWebauthnError as exc:
        raise _pw_error_to_http(exc) from exc

    token = make_principal_token(principal_id)
    logger.info("principal login OK: principal=%s (unbound)", principal_id)
    return PrincipalSessionResponse(
        access_token=token,
        expires_in=3600,
        principal_id=principal_id,
    )


# --------------------------------------------------------------------------- #
# Registration — authenticated principal adds a key.
# --------------------------------------------------------------------------- #


@router.post(
    "/auth/webauthn/register/begin", response_model=WebauthnBeginResponse
)
async def register_begin(
    ctx: PrincipalContext = Depends(require_principal_bearer),
) -> WebauthnBeginResponse:
    try:
        return WebauthnBeginResponse(
            **await pw.begin_registration(ctx.principal_id)
        )
    except pw.PrincipalWebauthnError as exc:
        raise _pw_error_to_http(exc) from exc


@router.post(
    "/auth/webauthn/register/finish", response_model=RegisterFinishResponse
)
async def register_finish(
    body: RegisterFinishRequest,
    ctx: PrincipalContext = Depends(require_principal_bearer),
) -> RegisterFinishResponse:
    try:
        result = await pw.complete_registration(
            ctx.principal_id, body.credential, body.friendly_name
        )
    except pw.PrincipalWebauthnError as exc:
        raise _pw_error_to_http(exc) from exc
    return RegisterFinishResponse(**result)


# --------------------------------------------------------------------------- #
# List actable tenants + act-as.
# --------------------------------------------------------------------------- #


@router.get("/tenants", response_model=list[ActableTenantOut])
async def list_tenants(
    ctx: PrincipalContext = Depends(require_principal_bearer),
) -> list[ActableTenantOut]:
    """Return the tenants this principal may act as (its own active grants).

    Reads via ``list_actable_tenants`` -> ``principal_visible_grants`` (the
    SECURITY DEFINER resolver) with the AUTHENTICATED principal's id. A
    principal can never enumerate another principal's grants — the id is taken
    from the session, never the request.
    """
    async with AsyncSessionLocal() as session:
        actable = await principal_svc.list_actable_tenants(
            session, ctx.principal_id
        )
    return [
        ActableTenantOut(
            tenant_id=t.tenant_id, role=t.role, grant_id=t.grant_id
        )
        for t in actable
    ]


@router.post("/act-as", response_model=PrincipalSessionResponse)
async def act_as(
    body: ActAsRequest,
    ctx: PrincipalContext = Depends(require_principal_bearer),
) -> PrincipalSessionResponse:
    """Bind the principal to ``tenant_id`` after verifying an active grant.

    Verifies the grant via ``resolve_grant_role`` under the app role; on
    success mints a TENANT-BOUND principal token. No active grant -> 403, no
    token, no binding. The bound token's subsequent queries run under the
    SAME FORCE-RLS path as a native user (``deps.get_session`` binds
    ``app.current_tenant`` from the claim — see that wiring).
    """
    async with AsyncSessionLocal() as session:
        role = await principal_svc.resolve_grant_role(
            session, ctx.principal_id, body.tenant_id
        )
    if role is None:
        # Fail closed; leak nothing about whether the tenant exists.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "no active grant for the requested tenant",
        )
    token = make_principal_token(
        ctx.principal_id, tenant_id=body.tenant_id, role=role
    )
    logger.info(
        "principal act-as OK: principal=%s tenant=%s role=%s",
        ctx.principal_id,
        body.tenant_id,
        role,
    )
    return PrincipalSessionResponse(
        access_token=token,
        expires_in=3600,
        principal_id=ctx.principal_id,
        tenant_id=body.tenant_id,
        role=role,
    )


# --------------------------------------------------------------------------- #
# Bound-session proof endpoint. A tenant-bound principal lists the contacts of
# the act-as tenant — the read runs under app.current_tenant + FORCE-RLS, so
# it returns ONLY the bound tenant's rows. This is the HTTP-level proof that
# act-as binding goes through the same isolation as a native user; the
# service-layer proof lives in test_principal_cross_tenant.py.
# --------------------------------------------------------------------------- #


class BoundContactOut(BaseModel):
    id: uuid.UUID
    name: str
    tenant_id: uuid.UUID


@router.get("/acting/contacts", response_model=list[BoundContactOut])
async def acting_contacts(
    session: AsyncSession = Depends(get_principal_tenant_session),
) -> list[BoundContactOut]:
    rows = (
        await session.execute(
            text(
                "SELECT id, name, tenant_id FROM contacts "
                "WHERE archived_at IS NULL ORDER BY name"
            )
        )
    ).all()
    return [
        BoundContactOut(id=r.id, name=r.name, tenant_id=r.tenant_id)
        for r in rows
    ]
