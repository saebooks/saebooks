"""Authoriser edge-enable flow for the IC REMOTE relay (Phase 3c).

The ONLY way a REMOTE edge becomes ``relay_status=ACTIVE``. Reuses the 0156
cross-tenant principal mechanism verbatim — NO new privileged data path, NO
BYPASSRLS. The authoriser (Richard, FIDO2-authenticated) must hold an ACTIVE
grant on BOTH the source and destination tenants; only then is it the authority
needed to wire the two halves of a reciprocal cross-DB edge — and nothing more.

What enabling an edge does (plan §4.1):
  1. assert the principal holds an active grant on src AND dst tenants
     (``principal_grant_role`` SECURITY DEFINER predicate, parameterised by the
     server-resolved principal id);
  2. assert the principal is FIDO2-satisfied (no code-2FA, standing rule);
  3. generate an Ed25519 keypair PER SIDE; store each private key Fernet-wrapped
     in that side's ``ic_edges.relay_privkey_ciphertext`` and the public key in
     the PARTNER's ``relay_pubkey``;
  4. mint a per-edge scoped token PER SIDE (api_token prefix+bcrypt shape);
  5. register the pair with the broker (public keys + token HASHES only — the
     broker never sees a private key or a token cleartext or any money);
  6. flip both edges to ACTIVE and stamp ``authorised_by_principal_id``.

This module does the in-DB half (steps 1-4, 6). The broker registration (step 5)
is an HTTP call the caller makes with the returned material; we return the
material rather than perform the call so the DB writes and the network call can
be ordered/retried by the orchestrating endpoint. Both edge rows live in
DIFFERENT tenant DBs, so the caller binds each session to its own tenant (via
``principal.bind_session_to_tenant``) before writing that side.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.models.ic import IcEdge, IcEdgeRelayStatus, IcEdgeTopology
from saebooks.models.principal import Principal
from saebooks.services import principal as principal_svc
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay import signing as relay_signing


class EdgeEnableError(Exception):
    """Raised when a REMOTE edge cannot be authorised for relay."""


class NotAuthorised(EdgeEnableError):
    """The principal does not hold the required dual-tenant grant / FIDO2.

    Deliberately opaque — does not say which tenant's grant is missing.
    """


@dataclass(frozen=True)
class SideKeys:
    """The freshly-minted material for one side of a REMOTE edge.

    ``token_cleartext`` is shown ONCE (it goes to the broker's secret store for
    presentation back to this side; never persisted in cleartext here).
    """

    public_key: bytes
    token_cleartext: str
    token_prefix: str


@dataclass(frozen=True)
class EnabledEdgePair:
    """Result of an edge-enable: the broker-registration material for both sides.

    The private keys are already persisted (Fernet-wrapped) in their tenant DBs;
    only the PUBLIC keys + token cleartexts leave for the broker registry.
    """

    edge_id: uuid.UUID
    src_tenant_id: uuid.UUID
    dst_tenant_id: uuid.UUID
    src: SideKeys
    dst: SideKeys


async def assert_dual_grant(
    session: AsyncSession,
    *,
    principal: Principal,
    src_tenant_id: uuid.UUID,
    dst_tenant_id: uuid.UUID,
) -> None:
    """Verify the principal may authorise a src<->dst REMOTE edge.

    Requires (a) at least one FIDO2 credential (``assert_fido2_satisfied`` —
    no code-2FA), and (b) an ACTIVE grant of sufficient privilege on BOTH
    tenants via the 0156 ``principal_grant_role`` SECURITY DEFINER predicate.
    Raises :class:`NotAuthorised` (opaque) on any failure. NO BYPASSRLS — the
    predicate is a parameterised SECURITY DEFINER function, exactly the
    "act-as" pattern.

    ``session`` must NOT be tenant-bound for this call (the grant predicate is a
    SECURITY DEFINER function that reads grants across tenants by principal id —
    binding app.current_tenant would scope it to one tenant). The caller passes
    a pre-auth / unbound session here, then binds per side for the writes.
    """
    try:
        await principal_svc.assert_fido2_satisfied(session, principal)
    except principal_svc.Fido2NotEnrolled as exc:
        raise NotAuthorised(
            "authoriser is not FIDO2-enrolled — cannot enable a cross-tenant edge"
        ) from exc

    _ALLOWED_ROLES = {"owner", "admin", "accountant"}
    src_role = await principal_svc.resolve_grant_role(
        session, principal.id, src_tenant_id
    )
    dst_role = await principal_svc.resolve_grant_role(
        session, principal.id, dst_tenant_id
    )
    if src_role is None or dst_role is None:
        raise NotAuthorised(
            "authoriser does not hold an active grant on both tenants"
        )
    if src_role not in _ALLOWED_ROLES or dst_role not in _ALLOWED_ROLES:
        raise NotAuthorised(
            "authoriser's grant role is insufficient to enable a relay edge"
        )


async def _enable_one_side(
    bound_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID,
    partner_tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    private_raw: bytes,
    partner_public_raw: bytes,
    settings: Settings,
) -> SideKeys:
    """Persist THIS side's signing key + partner pubkey + token; flip ACTIVE.

    ``bound_session`` MUST already be bound to ``tenant_id`` (the caller does
    this via ``principal.bind_session_to_tenant`` so the write is under this
    tenant's own FORCE-RLS — never BYPASSRLS). Stores this side's private key
    Fernet-wrapped, the partner's public key for verifying their inbound legs,
    and a fresh per-edge token (prefix + bcrypt hash). Returns the public key +
    token cleartext for the broker registry.
    """
    edge = (
        await bound_session.execute(
            select(IcEdge).where(
                IcEdge.id == edge_id,
                IcEdge.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise EdgeEnableError("edge not found for this tenant")
    if edge.topology != IcEdgeTopology.REMOTE:
        raise EdgeEnableError("only a REMOTE edge can be enabled for relay")

    this_public = relay_signing.public_key_for(private_raw)
    token_cleartext, token_prefix = relay_keys.generate_edge_token()

    edge.partner_tenant_id = partner_tenant_id
    edge.relay_privkey_ciphertext = relay_keys.wrap_private_key(
        private_raw, settings=settings
    ).encode("ascii")
    edge.relay_pubkey = partner_public_raw
    edge.relay_token_prefix = token_prefix
    edge.relay_token_hash = relay_keys.hash_edge_token(token_cleartext)
    edge.relay_status = IcEdgeRelayStatus.ACTIVE
    edge.authorised_by_principal_id = principal_id
    await bound_session.flush()

    return SideKeys(
        public_key=this_public,
        token_cleartext=token_cleartext,
        token_prefix=token_prefix,
    )


async def enable_edge_pair(
    *,
    principal: Principal,
    auth_session: AsyncSession,
    src_session: AsyncSession,
    dst_session: AsyncSession,
    src_tenant_id: uuid.UUID,
    dst_tenant_id: uuid.UUID,
    src_edge_id: uuid.UUID,
    dst_edge_id: uuid.UUID,
    settings: Settings | None = None,
) -> EnabledEdgePair:
    """Authorise + wire BOTH halves of a REMOTE edge. Returns broker material.

    ``auth_session`` is the unbound session used for the dual-grant check.
    ``src_session`` / ``dst_session`` MUST each be bound to their respective
    tenant (the caller binds them via ``principal.bind_session_to_tenant``). We
    assert authority FIRST, generate a keypair per side, persist each side's
    private key in its OWN tenant DB and the partner's public key for verifying
    inbound, flip both ACTIVE, and return the PUBLIC keys + token cleartexts the
    caller registers with the broker.

    No private key ever leaves its tenant; the broker only ever receives public
    keys and token hashes (computed by the broker from the cleartexts the caller
    hands it, or the cleartexts themselves over the internal-only LAN hop — the
    broker stores only the hash). The whole flow has NO BYPASSRLS data path.
    """
    cfg = settings if settings is not None else _default_settings

    await assert_dual_grant(
        auth_session,
        principal=principal,
        src_tenant_id=src_tenant_id,
        dst_tenant_id=dst_tenant_id,
    )

    src_priv, src_pub = relay_keys.new_signing_key()
    dst_priv, dst_pub = relay_keys.new_signing_key()

    # Source side stores its own private key + the destination's public key.
    src_material = await _enable_one_side(
        src_session,
        tenant_id=src_tenant_id,
        edge_id=src_edge_id,
        partner_tenant_id=dst_tenant_id,
        principal_id=principal.id,
        private_raw=src_priv,
        partner_public_raw=dst_pub,
        settings=cfg,
    )
    # Destination side stores its own private key + the source's public key.
    dst_material = await _enable_one_side(
        dst_session,
        tenant_id=dst_tenant_id,
        edge_id=dst_edge_id,
        partner_tenant_id=src_tenant_id,
        principal_id=principal.id,
        private_raw=dst_priv,
        partner_public_raw=src_pub,
        settings=cfg,
    )

    await src_session.commit()
    await dst_session.commit()

    return EnabledEdgePair(
        edge_id=src_edge_id,
        src_tenant_id=src_tenant_id,
        dst_tenant_id=dst_tenant_id,
        src=src_material,
        dst=dst_material,
    )
