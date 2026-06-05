"""Cross-tenant principal services — grant resolution + "act as tenant".

This module is the *only* sanctioned way a principal crosses a tenant
boundary. Read the security model in
``docs/security/accountant-principal.md`` before changing anything here.

The one rule
------------
``act_as_tenant`` binds ``app.current_tenant`` to a target tenant ONLY after
``principal_grant_role`` (a SECURITY DEFINER predicate, migration 0155)
confirms the principal holds an *active* grant for it. Once bound, the
principal's session is subject to the identical FORCE-RLS policies a native
user of that tenant gets. There is no ``BYPASSRLS`` path; a principal with no
grant for tenant C can never reach ``app.current_tenant = C`` through this
service, so RLS returns zero rows for C.

Resolution functions
--------------------
* ``list_actable_tenants(session, principal_id)`` — the "select tenant"
  dashboard data. Calls the SECURITY DEFINER ``principal_visible_grants`` so
  the principal can read its OWN active grants across every granting tenant
  without ``app.current_tenant`` scoping the read to one tenant.
* ``resolve_grant_role(session, principal_id, tenant_id)`` — the granted role
  for one (principal, tenant), or ``None`` if no active grant.
* ``bind_session_to_tenant(session, principal_id, tenant_id)`` — verify +
  bind; raises :class:`NoActiveGrant` if the principal may not act as the
  tenant. This is what the API layer calls.

FIDO2-only auth seam
--------------------
``assert_fido2_satisfied`` enforces the standing rule (no code-2FA): a
principal session may only be minted if the principal has at least one FIDO2
credential. Live WebAuthn ceremony is out of scope for this branch;
``enrol_fido2_credential`` documents the seam where a verified WebAuthn
registration response would be persisted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.principal import (
    Principal,
    PrincipalFido2Credential,
)


class PrincipalError(Exception):
    """Base error for cross-tenant principal operations."""


class NoActiveGrant(PrincipalError):
    """Raised when a principal tries to act as a tenant it has no grant for.

    The message deliberately does NOT reveal whether the tenant exists or
    whether some *other* principal has a grant to it — it only states that
    *this* principal may not act as *this* tenant. Fail closed, leak nothing.
    """


class Fido2NotEnrolled(PrincipalError):
    """Raised when a principal has no FIDO2 credential but requires one.

    A principal authenticates FIDO2-only. Until at least one credential is
    enrolled, no principal session may be minted.
    """


@dataclass(frozen=True)
class ActableTenant:
    """One tenant a principal may act as, with the scoped role it holds."""

    tenant_id: uuid.UUID
    role: str
    grant_id: uuid.UUID


# --------------------------------------------------------------------------- #
# Grant resolution — cross-tenant reads via SECURITY DEFINER functions.
# --------------------------------------------------------------------------- #


async def list_actable_tenants(
    session: AsyncSession, principal_id: uuid.UUID
) -> list[ActableTenant]:
    """Return every tenant this principal currently holds an active grant for.

    Reads through the SECURITY DEFINER ``principal_visible_grants`` function
    (migration 0155) so the principal can see its OWN active grants across
    all granting tenants. The function is parameterised by the single
    ``principal_id`` and filters to it; the caller MUST pass the
    *authenticated* principal's id, never a client-supplied value.

    No ``app.current_tenant`` is set for this call — that is the whole point:
    a tenant-scoped read would only ever return grants for the one bound
    tenant, which is not what the principal's dashboard needs.
    """
    rows = (
        await session.execute(
            text(
                "SELECT grant_id, tenant_id, role "
                "FROM principal_visible_grants(:pid)"
            ),
            {"pid": str(principal_id)},
        )
    ).all()
    return [
        ActableTenant(
            tenant_id=r.tenant_id, role=r.role, grant_id=r.grant_id
        )
        for r in rows
    ]


async def resolve_grant_role(
    session: AsyncSession,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> str | None:
    """Return the active scoped role for (principal, tenant), else ``None``.

    Calls the SECURITY DEFINER ``principal_grant_role`` predicate. Returns
    the role string when an active grant exists, ``None`` otherwise. The
    ``None`` case is the security gate: the caller must refuse to bind the
    tenant.
    """
    result = await session.execute(
        text("SELECT principal_grant_role(:pid, :tid) AS role"),
        {"pid": str(principal_id), "tid": str(tenant_id)},
    )
    row = result.first()
    return row.role if row is not None else None


# --------------------------------------------------------------------------- #
# "Act as tenant" — the single place the tenant boundary is crossed.
# --------------------------------------------------------------------------- #


async def bind_session_to_tenant(
    session: AsyncSession,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> str:
    """Verify an active grant, then bind the session to ``tenant_id``.

    On success the session's ``app.current_tenant`` GUC is set to the target
    tenant for the remainder of the current transaction, and the granted
    role is returned. Every subsequent query on this session is bound by the
    target tenant's FORCE-RLS policies — identical to a native user.

    Raises :class:`NoActiveGrant` if the principal holds no active grant for
    the tenant; in that case ``app.current_tenant`` is NOT set, so RLS keeps
    the principal at zero rows.

    Implementation note — the GUC binding here is a direct ``SET LOCAL`` on
    the *current* transaction, mirroring ``deps._set_current_tenant_on_begin``
    and ``api/v1/integrations.py``. The API layer is expected to also stamp
    ``session.info['tenant_id']`` (see ``saebooks.api.v1.principal_session``)
    so the ``after_begin`` listener re-issues the SET LOCAL after any commit,
    matching how ``get_session`` keeps the GUC alive across NullPool
    connection swaps. We assert the grant FIRST, before any binding, so a
    failed verification never leaves a partially-bound session.
    """
    role = await resolve_grant_role(session, principal_id, tenant_id)
    if role is None:
        raise NoActiveGrant(
            f"principal {principal_id} has no active grant for the requested "
            "tenant"
        )
    # Verified — bind the GUC for this transaction. UUIDs are validated by
    # type before interpolation (resolve_grant_role already round-tripped
    # them through asyncpg as parameters); SET does not accept bind params.
    tid = uuid.UUID(str(tenant_id))  # re-validate; raises on a bad value
    await session.execute(
        text(f"SET LOCAL app.current_tenant = '{tid}'")
    )
    # Keep the GUC alive across NullPool connection swaps after commit.
    session.info["tenant_id"] = str(tid)
    return role


# --------------------------------------------------------------------------- #
# FIDO2-only auth seam.
# --------------------------------------------------------------------------- #


async def assert_fido2_satisfied(
    session: AsyncSession, principal: Principal
) -> None:
    """Enforce the FIDO2-only rule before a principal session is minted.

    Raises :class:`Fido2NotEnrolled` when the principal requires FIDO2 (the
    default and only supported posture) but has no credential row. There is
    deliberately NO code-2FA fallback — that is a hard standing rule.

    This is the gate the (future) principal-login endpoint calls after a
    successful WebAuthn assertion: it confirms the principal is allowed to
    have a session at all. The WebAuthn assertion verification itself is the
    enrolment seam (see ``enrol_fido2_credential``).
    """
    if not principal.requires_fido2:
        # No real cross-tenant actor should ever have this false; we still
        # honour it defensively rather than silently downgrading security.
        return
    count = (
        await session.execute(
            select(PrincipalFido2Credential.id).where(
                PrincipalFido2Credential.principal_id == principal.id
            )
        )
    ).first()
    if count is None:
        raise Fido2NotEnrolled(
            f"principal {principal.id} has no FIDO2 credential enrolled; "
            "code-based 2FA is not permitted"
        )


async def enrol_fido2_credential(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    credential_id: bytes,
    public_key: bytes,
    sign_count: int = 0,
    transports: list[str] | None = None,
    friendly_name: str = "Security key",
) -> PrincipalFido2Credential:
    """Persist a verified FIDO2 credential for a principal (enrolment seam).

    THIS BRANCH DOES NOT IMPLEMENT THE LIVE WEBAUTHN CEREMONY. The seam is:
    a future ``POST /api/v1/principal/fido2/register`` endpoint runs the
    standard WebAuthn registration (attestation) ceremony using the same
    ``webauthn`` machinery as ``user_webauthn_credentials`` (see
    ``saebooks/services/webauthn`` and migration 0135). Once the attestation
    response is verified server-side, it calls THIS function with the
    extracted ``credential_id`` / ``public_key`` to persist the binding. No
    code-2FA path is ever wired.

    The function is written and tested so the persistence half is provable
    now; only the ceremony wiring is deferred. Callers must have already
    verified the attestation — this function trusts its inputs.
    """
    cred = PrincipalFido2Credential(
        principal_id=principal_id,
        credential_id=credential_id,
        public_key=public_key,
        sign_count=sign_count,
        transports=transports or [],
        friendly_name=friendly_name,
    )
    session.add(cred)
    await session.flush()
    return cred
