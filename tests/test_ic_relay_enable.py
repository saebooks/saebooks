"""Phase 3c authoriser edge-enable — dual-grant + FIDO2 + key wiring (no BYPASSRLS).

Proves ``saebooks.services.ic_relay.enable``:

* a principal holding an ACTIVE grant on BOTH tenants AND a FIDO2 credential can
  enable a REMOTE edge pair — both edges flip to ACTIVE, each side stores its OWN
  Fernet-wrapped private key + the PARTNER's public key + a per-edge token hash,
  and the returned material carries only PUBLIC keys + token cleartexts for the
  broker (NO private key ever leaves);
* a principal missing the grant on ONE tenant is refused (NotAuthorised) and
  NOTHING is wired — the no-BYPASSRLS authority gate;
* a FIDO2-less principal is refused (no code-2FA standing rule).

The grant predicate is the existing 0156 SECURITY DEFINER ``principal_grant_role``
— this test seeds real ``principal_tenant_grants`` rows and lets the predicate
decide, so it proves the reuse, not a reimplementation.

Postgres only (SECURITY DEFINER predicates + grants).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault(
    "SAEBOOKS_FIELD_ENCRYPTION_KEY",
    "c2FlYm9va3MtdGVzdC1rZXktZG8tbm90LXVzZS1wcm8=",
)

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import IcEdge, IcEdgeDirection, IcEdgeRelayStatus, IcEdgeTopology
from saebooks.models.principal import (
    Principal,
    PrincipalFido2Credential,
    PrincipalKind,
    PrincipalTenantGrant,
)
from saebooks.models.tenant import Tenant
from saebooks.services.ic_relay import enable as enable_svc
from saebooks.services.ic_relay import keys as relay_keys

pytestmark = pytest.mark.postgres_only


@pytest_asyncio.fixture
async def enable_setup() -> AsyncIterator[dict[str, Any]]:
    """Two tenants each with a REMOTE (INACTIVE) edge + a cross-tenant principal."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as s:
        src_t = Tenant(id=uuid.uuid4(), name=f"en-src-{tag}", slug=f"en-src-{tag}")
        dst_t = Tenant(id=uuid.uuid4(), name=f"en-dst-{tag}", slug=f"en-dst-{tag}")
        s.add_all([src_t, dst_t])
        await s.flush()
        src_co = Company(id=uuid.uuid4(), tenant_id=src_t.id, name=f"src-{tag}", base_currency="AUD")
        dst_co = Company(id=uuid.uuid4(), tenant_id=dst_t.id, name=f"dst-{tag}", base_currency="AUD")
        s.add_all([src_co, dst_co])
        await s.flush()
        src_acct = Account(id=uuid.uuid4(), tenant_id=src_t.id, company_id=src_co.id,
                           code=f"1-15{tag[:2]}", name="Loan", account_type=AccountType.ASSET)
        dst_acct = Account(id=uuid.uuid4(), tenant_id=dst_t.id, company_id=dst_co.id,
                           code=f"2-22{tag[:2]}", name="DLoan", account_type=AccountType.LIABILITY)
        s.add_all([src_acct, dst_acct])
        await s.flush()
        src_edge = IcEdge(id=uuid.uuid4(), tenant_id=src_t.id, company_id=src_co.id,
                          partner_company_id=None, control_account_id=src_acct.id,
                          direction=IcEdgeDirection.ORIGINATOR, topology=IcEdgeTopology.REMOTE,
                          relay_status=IcEdgeRelayStatus.INACTIVE)
        dst_edge = IcEdge(id=uuid.uuid4(), tenant_id=dst_t.id, company_id=dst_co.id,
                          partner_company_id=None, control_account_id=dst_acct.id,
                          direction=IcEdgeDirection.COUNTERPARTY, topology=IcEdgeTopology.REMOTE,
                          relay_status=IcEdgeRelayStatus.INACTIVE)
        s.add_all([src_edge, dst_edge])
        principal = Principal(
            id=uuid.uuid4(), kind=PrincipalKind.ACCOUNTANT.value,
            display_name=f"Accountant {tag}", username=f"acct-{tag}",
            email=f"acct-{tag}@example.com", requires_fido2=True,
        )
        s.add(principal)
        await s.flush()
        # FIDO2 credential so assert_fido2_satisfied passes.
        s.add(PrincipalFido2Credential(principal_id=principal.id,
                                       credential_id=b"cred-" + tag.encode(),
                                       public_key=b"pk", sign_count=0,
                                       transports=[], friendly_name="yk"))
        await s.commit()
        out = {
            "src_tenant": src_t.id, "dst_tenant": dst_t.id,
            "src_edge": src_edge.id, "dst_edge": dst_edge.id,
            "principal_id": principal.id,
        }
    yield out
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM principal_fido2_credentials WHERE principal_id = :p"),
                        {"p": out["principal_id"]})
        await s.execute(text("DELETE FROM principal_tenant_grants WHERE principal_id = :p"),
                        {"p": out["principal_id"]})
        await s.execute(text("DELETE FROM principals WHERE id = :p"), {"p": out["principal_id"]})
        for tbl in ("ic_edges", "accounts", "companies"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id IN (:a, :b)"),
                            {"a": out["src_tenant"], "b": out["dst_tenant"]})
        await s.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                        {"a": out["src_tenant"], "b": out["dst_tenant"]})
        await s.commit()


async def _grant(session, principal_id, tenant_id, role="accountant", status="active") -> None:
    session.add(PrincipalTenantGrant(
        id=uuid.uuid4(), principal_id=principal_id, tenant_id=tenant_id,
        role=role, status=status,
    ))


async def _bind(session, tenant_id) -> None:
    session.info["tenant_id"] = str(tenant_id)
    async with session.begin():
        await session.execute(text(f"SET LOCAL app.current_tenant = '{tenant_id}'"))


async def test_dual_grant_enables_both_edges(enable_setup: dict[str, Any]) -> None:
    d = enable_setup
    # Seed grants on BOTH tenants.
    async with AsyncSessionLocal() as s:
        await _grant(s, d["principal_id"], d["src_tenant"])
        await _grant(s, d["principal_id"], d["dst_tenant"])
        await s.commit()

    async with AsyncSessionLocal() as auth_s, \
            AsyncSessionLocal() as src_s, AsyncSessionLocal() as dst_s:
        principal = (await auth_s.execute(
            select(Principal).where(Principal.id == d["principal_id"])
        )).scalar_one()
        await _bind(src_s, d["src_tenant"])
        await _bind(dst_s, d["dst_tenant"])
        result = await enable_svc.enable_edge_pair(
            principal=principal, auth_session=auth_s,
            src_session=src_s, dst_session=dst_s,
            src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
            src_edge_id=d["src_edge"], dst_edge_id=d["dst_edge"],
        )

    # Both edges ACTIVE, each with its OWN privkey + the partner's pubkey.
    async with AsyncSessionLocal() as s:
        src_edge = (await s.execute(select(IcEdge).where(IcEdge.id == d["src_edge"]))).scalar_one()
        dst_edge = (await s.execute(select(IcEdge).where(IcEdge.id == d["dst_edge"]))).scalar_one()
    assert src_edge.relay_status == IcEdgeRelayStatus.ACTIVE
    assert dst_edge.relay_status == IcEdgeRelayStatus.ACTIVE
    assert src_edge.partner_tenant_id == d["dst_tenant"]
    assert dst_edge.partner_tenant_id == d["src_tenant"]
    assert src_edge.authorised_by_principal_id == d["principal_id"]
    # Cross-wired keys: src verifies dst inbound -> src.relay_pubkey == dst's public.
    assert src_edge.relay_pubkey == result.dst.public_key
    assert dst_edge.relay_pubkey == result.src.public_key
    # Private keys are present + Fernet-wrapped (decrypt round-trips, never raw).
    from saebooks.services.ic_relay import signing as _sig
    src_priv = relay_keys.unwrap_private_key(src_edge.relay_privkey_ciphertext.decode("ascii"))
    assert _sig.public_key_for(src_priv) == result.src.public_key
    # Tokens issued (hash stored, cleartext returned for the broker).
    assert result.src.token_cleartext.startswith("icrl_")
    assert relay_keys.verify_edge_token(result.src.token_cleartext, src_edge.relay_token_hash)


async def test_missing_one_grant_is_refused_and_wires_nothing(
    enable_setup: dict[str, Any]
) -> None:
    d = enable_setup
    # Grant ONLY the src tenant — dst is missing.
    async with AsyncSessionLocal() as s:
        await _grant(s, d["principal_id"], d["src_tenant"])
        await s.commit()

    async with AsyncSessionLocal() as auth_s, \
            AsyncSessionLocal() as src_s, AsyncSessionLocal() as dst_s:
        principal = (await auth_s.execute(
            select(Principal).where(Principal.id == d["principal_id"])
        )).scalar_one()
        await _bind(src_s, d["src_tenant"])
        await _bind(dst_s, d["dst_tenant"])
        with pytest.raises(enable_svc.NotAuthorised):
            await enable_svc.enable_edge_pair(
                principal=principal, auth_session=auth_s,
                src_session=src_s, dst_session=dst_s,
                src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
                src_edge_id=d["src_edge"], dst_edge_id=d["dst_edge"],
            )
    # Nothing wired — both edges still INACTIVE.
    async with AsyncSessionLocal() as s:
        src_edge = (await s.execute(select(IcEdge).where(IcEdge.id == d["src_edge"]))).scalar_one()
        dst_edge = (await s.execute(select(IcEdge).where(IcEdge.id == d["dst_edge"]))).scalar_one()
    assert src_edge.relay_status == IcEdgeRelayStatus.INACTIVE
    assert dst_edge.relay_status == IcEdgeRelayStatus.INACTIVE


async def test_no_fido2_is_refused(enable_setup: dict[str, Any]) -> None:
    d = enable_setup
    # Grant both, but strip the FIDO2 credential -> no-code-2FA rule refuses.
    async with AsyncSessionLocal() as s:
        await _grant(s, d["principal_id"], d["src_tenant"])
        await _grant(s, d["principal_id"], d["dst_tenant"])
        await s.execute(text("DELETE FROM principal_fido2_credentials WHERE principal_id = :p"),
                        {"p": d["principal_id"]})
        await s.commit()
    async with AsyncSessionLocal() as auth_s:
        principal = (await auth_s.execute(
            select(Principal).where(Principal.id == d["principal_id"])
        )).scalar_one()
        with pytest.raises(enable_svc.NotAuthorised):
            await enable_svc.assert_dual_grant(
                auth_s, principal=principal,
                src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
            )
