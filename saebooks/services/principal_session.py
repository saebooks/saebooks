"""Principal session tokens — the authenticated cross-tenant identity.

A **principal session** is a JWT that is *deliberately a different shape* from
a user JWT (``saebooks.services.jwt_tokens.make_access_token``):

* a user token carries ``sub`` (user id) + ``tenant_id`` and is consumed by
  ``saebooks.api.v1.auth.require_bearer`` + ``deps.get_session``;
* a principal token carries ``psub`` (principal id) + ``typ="principal"`` and
  is consumed ONLY by ``require_principal_bearer``
  (``saebooks.api.v1.principal_auth``).

The two paths never cross:

* ``decode_principal_token`` rejects anything whose ``typ`` is not
  ``"principal"``, so a *user* JWT can never authenticate a principal
  endpoint.
* ``require_principal_bearer`` rejects a principal token on a user endpoint
  implicitly — user endpoints decode with ``decode_access_token`` and key on
  ``sub``; a principal token has no ``sub``.

This keeps the **existing single-tenant user login/enforcement path
byte-for-byte unchanged** while adding the principal path alongside it.

Two principal-token states
--------------------------
1. **Unbound** — minted at login (after a verified FIDO2 assertion). Carries
   ``psub`` + ``typ`` only. It authenticates the principal but is NOT bound to
   any tenant: it may call ``/principal/tenants`` (list grants) and
   ``/principal/act-as`` (request a binding), nothing else tenant-scoped.
2. **Tenant-bound** — minted by ``/principal/act-as`` AFTER
   ``resolve_grant_role`` confirms an *active* grant. Adds ``tenant_id`` +
   ``role``. The session dependency binds ``app.current_tenant`` from this
   claim, so every query runs under the SAME FORCE-RLS as a native user. The
   ``psub`` is preserved for audit attribution.

The critical invariant
-----------------------
``psub`` is set ONLY from a value the caller derived server-side from a
verified credential (login) or from the already-authenticated principal
session (act-as). It is NEVER taken from a client-supplied request parameter.
The mint helpers here just stamp whatever id the caller passes; the *callers*
(``principal_webauthn.complete_authentication`` and the act-as endpoint) are
responsible for only ever passing a server-derived id. See
``docs/security/accountant-principal.md`` §10.
"""
from __future__ import annotations

import uuid
from typing import Any

from saebooks.services.jwt_tokens import (
    JWTError,
    create_access_token,
    decode_access_token,
)

# Token type marker. A token without exactly this ``typ`` is not a principal
# session and is rejected by ``decode_principal_token``.
PRINCIPAL_TOKEN_TYPE = "principal"

# Principal sessions are short-lived. The accountant re-taps the key when it
# expires; an unbound principal token does not warrant an 8h window.
_PRINCIPAL_TTL = 60 * 60  # 1 hour


class PrincipalTokenError(JWTError):
    """Raised when a token is not a valid principal session token."""


def make_principal_token(
    principal_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    role: str | None = None,
    expires_in_seconds: int = _PRINCIPAL_TTL,
) -> str:
    """Mint a principal session JWT.

    ``principal_id`` MUST be server-derived (the resolved credential's owner
    at login, or the authenticated principal's id at act-as) — never a client
    value. When ``tenant_id``/``role`` are given the token is *tenant-bound*
    (act-as); otherwise it is *unbound* (post-login).
    """
    payload: dict[str, Any] = {
        "psub": str(principal_id),
        "typ": PRINCIPAL_TOKEN_TYPE,
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    if role is not None:
        payload["role"] = role
    return create_access_token(payload, expires_in_seconds=expires_in_seconds)


def decode_principal_token(token: str) -> dict[str, Any]:
    """Verify + decode a principal session JWT.

    Raises :class:`PrincipalTokenError` if the signature/expiry is invalid OR
    if the token is not a principal token (wrong/missing ``typ``, missing
    ``psub``). The ``typ`` guard is what stops a normal *user* JWT — which has
    a valid signature but ``typ`` absent — from ever authenticating a
    principal endpoint.
    """
    try:
        claims = decode_access_token(token)
    except JWTError as exc:
        raise PrincipalTokenError(str(exc)) from exc
    if claims.get("typ") != PRINCIPAL_TOKEN_TYPE:
        raise PrincipalTokenError("not a principal token")
    psub = claims.get("psub")
    if not psub:
        raise PrincipalTokenError("principal token missing psub")
    try:
        uuid.UUID(str(psub))
    except (ValueError, TypeError) as exc:
        raise PrincipalTokenError("principal token psub is not a UUID") from exc
    return claims
