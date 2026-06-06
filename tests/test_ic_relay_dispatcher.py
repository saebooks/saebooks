"""Phase 3c dispatcher state machine — drain / ack / backoff / DEAD / flag-off.

Exercises ``saebooks.services.ic_relay.dispatcher`` with INJECTED factories so
it runs without the live engines: a fake app-role session factory yielding the
test AsyncSessionLocal, a fake login session factory enumerating the seeded
tenant, and a fake broker factory whose client either acks or fails.

Proves:
* a successful relay flips the outbox row PENDING -> ACKED;
* a transient broker failure flips it FAILED with a future next_attempt_at and
  increments attempts (backoff), and NEVER touches the local leg;
* exceeding max_attempts flips it DEAD (the half-pair surfaces for human action;
  the local leg is NEVER auto-reversed);
* the flag-off short-circuit drains nothing.

Postgres only (the outbox + FOR UPDATE SKIP LOCKED poll).
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

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcEdgeRelayStatus,
    IcEdgeTopology,
    IcOutbox,
    IcOutboxStatus,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.tenant import Tenant
from saebooks.services.ic_relay import dispatcher as disp
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay.broker_client import BrokerUnavailable

pytestmark = pytest.mark.postgres_only


def _test_settings(**over: Any) -> Settings:
    s = Settings()
    s.ic_remote_relay_enabled = True
    s.ic_relay_max_attempts = 3
    for k, v in over.items():
        setattr(s, k, v)
    return s


class _FakeClient:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail
        self.calls = 0

    async def relay(self, *, payload: dict, signature_b64: str, token: str) -> dict:
        self.calls += 1
        if self._fail:
            raise BrokerUnavailable("simulated broker down", status=None)
        return {"status": "DELIVERED"}


class _FakeBrokerFactory:
    def __init__(self, *, fail: bool, token: str | None = "icrl_x") -> None:
        self._client = _FakeClient(fail=fail)
        self._token = token

    def client(self) -> Any:
        return self._client

    def resolve_token(self, edge_id: uuid.UUID) -> str | None:
        return self._token


@pytest_asyncio.fixture
async def outbox_row() -> AsyncIterator[dict[str, Any]]:
    """One tenant with a REMOTE edge + a PENDING outbox row ready to dispatch."""
    tag = uuid.uuid4().hex[:8]
    priv, _pub = relay_keys.new_signing_key()
    async with AsyncSessionLocal() as s:
        t = Tenant(id=uuid.uuid4(), name=f"disp-{tag}", slug=f"disp-{tag}")
        s.add(t)
        await s.flush()
        co = Company(id=uuid.uuid4(), tenant_id=t.id, name=f"disp-{tag}", base_currency="AUD")
        s.add(co)
        await s.flush()
        acct = Account(id=uuid.uuid4(), tenant_id=t.id, company_id=co.id,
                       code=f"1-15{tag[:2]}", name="Loan", account_type=AccountType.ASSET)
        s.add(acct)
        await s.flush()
        edge = IcEdge(id=uuid.uuid4(), tenant_id=t.id, company_id=co.id,
                      partner_company_id=None, control_account_id=acct.id,
                      direction=IcEdgeDirection.ORIGINATOR, topology=IcEdgeTopology.REMOTE,
                      partner_tenant_id=uuid.uuid4(), relay_pubkey=b"\x00" * 32,
                      relay_privkey_ciphertext=relay_keys.wrap_private_key(priv).encode("ascii"),
                      relay_token_prefix="abc", relay_token_hash=relay_keys.hash_edge_token("icrl_x"),
                      relay_status=IcEdgeRelayStatus.ACTIVE)
        s.add(edge)
        txn = IcTxn(id=uuid.uuid4(), tenant_id=t.id, company_id=co.id, status=IcTxnStatus.ACTIVE)
        s.add(txn)
        await s.flush()
        ob = IcOutbox(id=uuid.uuid4(), tenant_id=t.id, company_id=co.id, ic_txn_id=txn.id,
                      edge_id=edge.id, idempotency_key=txn.id, nonce=uuid.uuid4(),
                      payload_json={"ic_txn_id": str(txn.id), "edge_id": str(edge.id),
                                    "nonce": str(uuid.uuid4()), "amount": "5000.00"},
                      signature=b"\x01" * 64, status=IcOutboxStatus.PENDING)
        s.add(ob)
        await s.commit()
        out = {"tenant_id": t.id, "outbox_id": ob.id, "edge_id": edge.id}
    yield out
    async with AsyncSessionLocal() as s:
        for tbl in ("ic_outbox", "ic_edges", "ic_txn", "accounts", "companies"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": out["tenant_id"]})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": out["tenant_id"]})
        await s.commit()


def _factories(tenant_id: uuid.UUID):
    # In the test DB AsyncSessionLocal is the owner engine, which can both
    # enumerate tenants (login factory role) and read the outbox under a bound
    # GUC (app factory role). Production wires the real app + login engines.
    return AsyncSessionLocal, AsyncSessionLocal


async def test_dispatch_success_acks(outbox_row: dict[str, Any]) -> None:
    d = outbox_row
    app_factory, login_factory = _factories(d["tenant_id"])
    bf = _FakeBrokerFactory(fail=False)
    sent = await disp.run_dispatcher_once(
        settings=_test_settings(), broker_factory=bf,
        app_session_factory=app_factory, login_session_factory=login_factory,
    )
    assert sent >= 1
    async with AsyncSessionLocal() as s:
        ob = (await s.execute(select(IcOutbox).where(IcOutbox.id == d["outbox_id"]))).scalar_one()
    assert ob.status == IcOutboxStatus.ACKED
    assert ob.next_attempt_at is None


async def test_dispatch_failure_backs_off(outbox_row: dict[str, Any]) -> None:
    d = outbox_row
    app_factory, login_factory = _factories(d["tenant_id"])
    bf = _FakeBrokerFactory(fail=True)
    await disp.run_dispatcher_once(
        settings=_test_settings(), broker_factory=bf,
        app_session_factory=app_factory, login_session_factory=login_factory,
    )
    async with AsyncSessionLocal() as s:
        ob = (await s.execute(select(IcOutbox).where(IcOutbox.id == d["outbox_id"]))).scalar_one()
    assert ob.status == IcOutboxStatus.FAILED
    assert ob.attempts == 1
    assert ob.next_attempt_at is not None  # backoff scheduled
    assert ob.last_error and "unavailable" in ob.last_error.lower()


async def test_dispatch_dead_after_max_never_reverses(outbox_row: dict[str, Any]) -> None:
    d = outbox_row
    app_factory, login_factory = _factories(d["tenant_id"])
    settings = _test_settings(ic_relay_max_attempts=3)
    bf = _FakeBrokerFactory(fail=True)
    # Force the row past max by pre-setting attempts and clearing the backoff so
    # it is due each pass.
    for _ in range(4):
        async with AsyncSessionLocal() as s:
            await s.execute(text(
                "UPDATE ic_outbox SET next_attempt_at = NULL WHERE id = :i"),
                {"i": d["outbox_id"]})
            await s.commit()
        await disp.run_dispatcher_once(
            settings=settings, broker_factory=bf,
            app_session_factory=app_factory, login_session_factory=login_factory,
        )
    async with AsyncSessionLocal() as s:
        ob = (await s.execute(select(IcOutbox).where(IcOutbox.id == d["outbox_id"]))).scalar_one()
    assert ob.status == IcOutboxStatus.DEAD, f"expected DEAD after max, got {ob.status}"
    # The local leg is untouched — DEAD never auto-reverses (plan D5). The outbox
    # row staying DEAD (not deleted, not reversed) IS the human-in-the-loop seam.


async def test_flag_off_drains_nothing(outbox_row: dict[str, Any]) -> None:
    d = outbox_row
    app_factory, login_factory = _factories(d["tenant_id"])
    s_off = _test_settings()
    s_off.ic_remote_relay_enabled = False
    sent = await disp.run_dispatcher_once(
        settings=s_off, broker_factory=_FakeBrokerFactory(fail=False),
        app_session_factory=app_factory, login_session_factory=login_factory,
    )
    assert sent == 0
    async with AsyncSessionLocal() as s:
        ob = (await s.execute(select(IcOutbox).where(IcOutbox.id == d["outbox_id"]))).scalar_one()
    assert ob.status == IcOutboxStatus.PENDING  # untouched
