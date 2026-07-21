"""Phase 3c LIVE relay — two-stack loop + signature/replay/freshness/flag-off.

The behavioural heart of the cross-DB intercompany relay. Both "stacks" are two
tenants in the ONE test DB (each under its own FORCE-RLS), which is sufficient to
prove the protocol: the originator posts a REMOTE leg + outbox row in one local
txn; a (simulated-broker) dispatcher relays the signed payload to the receiver's
``/ic/accept``; the receiver verifies + posts its reciprocal leg + inbox row in
one local txn; both sides are linked by the shared ``ic_txn_id``.

What is proven here
-------------------
* a remote pair LINKS by ``ic_txn_id`` (originator outbox + receiver inbox carry
  the same id; the receiver's local ic_txn is distinct but the inbox.ic_txn_id
  is the shared external id);
* the Dr/Cr on each side is the directors-loan convention, GST = 0 both legs;
* idempotent re-delivery posts nothing the second time (returns the prior ack);
* a tampered body, a replayed nonce, a stale message, and a wrong token are all
  rejected and post nothing;
* with the flag OFF, the originator post raises and ``/ic/accept`` returns 503;
* ``/ic/accept`` only ever writes the RECEIVER's own tenant (cross-tenant probe).

Postgres only (RLS + the app-role webhook path).
"""
from __future__ import annotations

import os
import uuid
from base64 import b64encode
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault(
    "SAEBOOKS_FIELD_ENCRYPTION_KEY",
    "c2FlYm9va3MtdGVzdC1rZXktZG8tbm90LXVzZS1wcm8=",
)

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcEdgeRelayStatus,
    IcEdgeTopology,
    IcInbox,
    IcOutbox,
    IcOutboxStatus,
)
from saebooks.models.journal import JournalEntry, JournalLine, JournalOrigin
from saebooks.models.tenant import Tenant
from saebooks.services import intercompany as ic_svc
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay import protocol as relay_protocol
from saebooks.services.ic_relay import signing as relay_signing

pytestmark = pytest.mark.postgres_only


@pytest.fixture
def relay_on() -> AsyncIterator[None]:
    """Turn the relay flag ON for the duration of a test (restored after)."""
    prev = settings.ic_remote_relay_enabled
    settings.ic_remote_relay_enabled = True
    yield
    settings.ic_remote_relay_enabled = prev


@pytest_asyncio.fixture
async def remote_pair() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each a company + control + contra account + an enabled REMOTE edge.

    Mirrors the §5 directors-loan edge as a cross-DB pair (two tenants here):
      SRC (personal): ORIGINATOR edge, control = "Loan to SAE" (ASSET),
                     contra = personal bank; signs with src priv; verifies dst.
      DST (primary):   COUNTERPARTY edge, control = "Directors Loan" (LIAB),
                     contra = SAE clearing; signs with dst priv; verifies src.
    Keys are wired exactly as the authoriser flow would (per-side keypair, this
    side's private Fernet-wrapped, partner's public, per-edge token hash).
    """
    tag = uuid.uuid4().hex[:8]
    src_priv, src_pub = relay_keys.new_signing_key()
    dst_priv, dst_pub = relay_keys.new_signing_key()
    src_token, src_prefix = relay_keys.generate_edge_token()
    dst_token, dst_prefix = relay_keys.generate_edge_token()

    out: dict[str, Any] = {"src_token": src_token, "dst_token": dst_token}
    async with AsyncSessionLocal() as s:
        src_t = Tenant(id=uuid.uuid4(), name=f"relay-src-{tag}", slug=f"relay-src-{tag}")
        dst_t = Tenant(id=uuid.uuid4(), name=f"relay-dst-{tag}", slug=f"relay-dst-{tag}")
        s.add_all([src_t, dst_t])
        await s.flush()

        src_co = Company(id=uuid.uuid4(), tenant_id=src_t.id, name=f"Richard-{tag}", base_currency="AUD")
        dst_co = Company(id=uuid.uuid4(), tenant_id=dst_t.id, name=f"SAE-{tag}", base_currency="AUD")
        s.add_all([src_co, dst_co])
        await s.flush()

        src_control = Account(id=uuid.uuid4(), tenant_id=src_t.id, company_id=src_co.id,
                              code=f"1-15{tag[:2]}", name="Loan to SAE", account_type=AccountType.ASSET)
        src_contra = Account(id=uuid.uuid4(), tenant_id=src_t.id, company_id=src_co.id,
                             code=f"1-10{tag[:2]}", name="Personal Bank", account_type=AccountType.ASSET)
        dst_control = Account(id=uuid.uuid4(), tenant_id=dst_t.id, company_id=dst_co.id,
                              code=f"2-22{tag[:2]}", name="Directors Loan", account_type=AccountType.LIABILITY)
        dst_contra = Account(id=uuid.uuid4(), tenant_id=dst_t.id, company_id=dst_co.id,
                             code=f"1-10{tag[:2]}", name="SAE Clearing", account_type=AccountType.ASSET)
        s.add_all([src_control, src_contra, dst_control, dst_contra])
        await s.flush()

        src_edge = IcEdge(
            id=uuid.uuid4(), tenant_id=src_t.id, company_id=src_co.id,
            partner_company_id=None, control_account_id=src_control.id,
            direction=IcEdgeDirection.ORIGINATOR, topology=IcEdgeTopology.REMOTE,
            partner_tenant_id=dst_t.id,
            relay_privkey_ciphertext=relay_keys.wrap_private_key(src_priv).encode("ascii"),
            relay_pubkey=dst_pub,  # we verify the DST's inbound with this
            relay_token_prefix=src_prefix,
            relay_token_hash=relay_keys.hash_edge_token(src_token),
            relay_status=IcEdgeRelayStatus.ACTIVE,
            relay_contra_account_id=src_contra.id,
        )
        dst_edge = IcEdge(
            id=uuid.uuid4(), tenant_id=dst_t.id, company_id=dst_co.id,
            partner_company_id=None, control_account_id=dst_control.id,
            direction=IcEdgeDirection.COUNTERPARTY, topology=IcEdgeTopology.REMOTE,
            partner_tenant_id=src_t.id,
            relay_privkey_ciphertext=relay_keys.wrap_private_key(dst_priv).encode("ascii"),
            relay_pubkey=src_pub,  # the receiver verifies the SRC's signature with this
            relay_token_prefix=dst_prefix,
            relay_token_hash=relay_keys.hash_edge_token(dst_token),
            relay_status=IcEdgeRelayStatus.ACTIVE,
            relay_contra_account_id=dst_contra.id,
        )
        s.add_all([src_edge, dst_edge])
        await s.commit()

        out.update(
            src_tenant=src_t.id, dst_tenant=dst_t.id,
            src_co=src_co.id, dst_co=dst_co.id,
            src_edge=src_edge.id, dst_edge=dst_edge.id,
            src_control=src_control.id, src_contra=src_contra.id,
            dst_control=dst_control.id, dst_contra=dst_contra.id,
        )

    yield out

    async with AsyncSessionLocal() as s:
        # journal_lines is keyed by company_id (filled by the 0152 trigger), not
        # tenant_id; delete it by company. The rest are tenant-scoped.
        await s.execute(
            text("DELETE FROM journal_lines WHERE company_id IN (:a, :b)"),
            {"a": out["src_co"], "b": out["dst_co"]},
        )
        for tbl in ("ic_inbox", "ic_outbox", "ic_legs", "ic_txn", "ic_edges",
                    "journal_entries", "accounts", "companies"):
            await s.execute(
                text(f"DELETE FROM {tbl} WHERE tenant_id IN (:a, :b)"),
                {"a": out["src_tenant"], "b": out["dst_tenant"]},
            )
        await s.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                        {"a": out["src_tenant"], "b": out["dst_tenant"]})
        await s.commit()


async def _post_originator(d: dict[str, Any], amount: str = "5000.00") -> tuple[Any, Any]:
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = str(d["src_tenant"])
        async with s.begin():
            await s.execute(text(f"SET LOCAL app.current_tenant = '{d['src_tenant']}'"))
        ic_txn, outbox = await ic_svc.post_remote_originator(
            s,
            tenant_id=d["src_tenant"],
            originator_company_id=d["src_co"],
            edge_id=d["src_edge"],
            amount=Decimal(amount),
            entry_date=date(2026, 6, 6),
            description="Director funds SAE working capital",
            posted_by="test",
        )
    return ic_txn, outbox


def _envelope_from_outbox(outbox: IcOutbox) -> dict[str, Any]:
    return {
        "payload": dict(outbox.payload_json),
        "signature": b64encode(bytes(outbox.signature)).decode("ascii"),
    }


# ----------------------------------------------------------------------------- #
# The two-stack loop
# ----------------------------------------------------------------------------- #
async def test_remote_pair_links_by_ic_txn_id(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair

    # 1. Originator posts -> local leg + outbox row, ONE txn.
    ic_txn, outbox = await _post_originator(d)
    assert outbox.status == IcOutboxStatus.PENDING
    assert outbox.ic_txn_id == ic_txn.id
    shared_id = uuid.UUID(outbox.payload_json["ic_txn_id"])
    assert shared_id == ic_txn.id, "originator's payload carries the shared id"

    # Originator leg: Dr control (Loan to SAE), Cr contra (Personal Bank). GST 0.
    async with AsyncSessionLocal() as s:
        ctrl = (await s.execute(
            select(JournalLine).where(JournalLine.account_id == d["src_control"])
        )).scalar_one()
        assert ctrl.debit == Decimal("5000.00") and ctrl.credit == Decimal("0")
        all_lines = (await s.execute(
            select(JournalLine).where(JournalLine.company_id == d["src_co"])
        )).scalars().all()
        assert len(all_lines) == 2, "originator leg is exactly 2 lines (no GST)"

    # 2. Simulate the broker forwarding the SAME signed envelope to /ic/accept.
    from saebooks.main import app
    env = _envelope_from_outbox(outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept",
            json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "POSTED"

    # 3. Receiver inbox carries the SHARED id; reciprocal leg posted.
    async with AsyncSessionLocal() as s:
        inbox = (await s.execute(
            select(IcInbox).where(IcInbox.tenant_id == d["dst_tenant"])
        )).scalar_one()
        assert inbox.ic_txn_id == shared_id, "receiver links by the shared ic_txn_id"
        assert inbox.journal_entry_id is not None

        # Receiver leg: Cr control (Directors Loan), Dr contra (SAE Clearing). GST 0.
        dctrl = (await s.execute(
            select(JournalLine).where(JournalLine.account_id == d["dst_control"])
        )).scalar_one()
        assert dctrl.credit == Decimal("5000.00") and dctrl.debit == Decimal("0")
        dst_lines = (await s.execute(
            select(JournalLine).where(JournalLine.company_id == d["dst_co"])
        )).scalars().all()
        assert len(dst_lines) == 2, "receiver leg is exactly 2 lines (no GST)"
        # Both legs stamped origin=INTERCOMPANY.
        je = (await s.execute(
            select(JournalEntry).where(JournalEntry.id == inbox.journal_entry_id)
        )).scalar_one()
        assert je.origin == JournalOrigin.INTERCOMPANY


async def test_idempotent_redelivery_posts_nothing_twice(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    from saebooks.main import app
    env = _envelope_from_outbox(outbox)
    headers = {"X-Tenant-Id": str(d["dst_tenant"]),
               "Authorization": f"Bearer {d['dst_token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r1 = await ac.post("/api/v1/intercompany/accept", json=env, headers=headers)
        r2 = await ac.post("/api/v1/intercompany/accept", json=env, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    # The genuine first POST is NOT a duplicate; the re-delivery IS flagged one,
    # so a broker/dispatcher can never count the replay as a fresh delivery.
    assert r1.json().get("duplicate") is False, "first delivery must not be flagged duplicate"
    assert r2.json().get("duplicate") is True, "re-delivery must be flagged duplicate"
    # Exactly ONE inbox row + ONE reciprocal leg despite two deliveries.
    async with AsyncSessionLocal() as s:
        n_inbox = (await s.execute(text(
            "SELECT count(*) FROM ic_inbox WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
        n_legs = (await s.execute(text(
            "SELECT count(*) FROM ic_legs WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
    assert n_inbox == 1, f"idempotency broken: {n_inbox} inbox rows"
    assert n_legs == 1, f"idempotency broken: {n_legs} reciprocal legs"


async def test_tampered_body_rejected_posts_nothing(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    env = _envelope_from_outbox(outbox)
    env["payload"] = dict(env["payload"], amount="9999.00")  # tamper after signing
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 400
    async with AsyncSessionLocal() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM ic_inbox WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
    assert n == 0, "a tampered message must post nothing"


async def test_replayed_nonce_rejected(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    """A second message with a DIFFERENT ic_txn_id but the SAME nonce is a replay."""
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    from saebooks.main import app
    headers = {"X-Tenant-Id": str(d["dst_tenant"]),
               "Authorization": f"Bearer {d['dst_token']}"}
    env = _envelope_from_outbox(outbox)
    # First delivery posts.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r1 = await ac.post("/api/v1/intercompany/accept", json=env, headers=headers)
        assert r1.status_code == 200
        # Forge a new ic_txn_id but reuse the nonce -> the nonce replay guard.
        # Re-sign so the signature is valid for the forged body (proves the
        # nonce UNIQUE constraint, not just the signature, blocks the replay).
        forged = dict(env["payload"], ic_txn_id=str(uuid.uuid4()))
        priv = relay_keys.unwrap_private_key(
            await _edge_privkey(d["src_tenant"], d["src_edge"])
        )
        sig = relay_signing.sign(relay_signing.canonical_payload(forged), priv)
        forged_env = {"payload": forged, "signature": b64encode(sig).decode("ascii")}
        r2 = await ac.post("/api/v1/intercompany/accept", json=forged_env, headers=headers)
    # The forged replay must not create a second posted leg.
    async with AsyncSessionLocal() as s:
        n_legs = (await s.execute(text(
            "SELECT count(*) FROM ic_legs WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
        n_inbox = (await s.execute(text(
            "SELECT count(*) FROM ic_inbox WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
    assert n_legs == 1, f"nonce replay produced {n_legs} legs (must stay 1)"
    assert n_inbox == 1, f"nonce replay produced {n_inbox} inbox rows (must stay 1)"
    # The replay response must be DISTINGUISHABLE from a genuine first POST: it is
    # either a flat reject (400) OR a 200 explicitly flagged ``duplicate`` — never
    # a bare 200 that a broker/dispatcher could count as a fresh delivery (the
    # false-positive-delivery-ack defect). A bare unflagged 200 here fails.
    assert r2.status_code in (400, 200), r2.text
    if r2.status_code == 200:
        assert r2.json().get("duplicate") is True, (
            "a replayed-nonce delivery returned a bare 200 with no duplicate flag "
            "— indistinguishable from a genuine first POST"
        )


async def _edge_privkey(tenant_id: uuid.UUID, edge_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = str(tenant_id)
        async with s.begin():
            await s.execute(text(f"SET LOCAL app.current_tenant = '{tenant_id}'"))
            edge = (await s.execute(
                select(IcEdge).where(IcEdge.id == edge_id)
            )).scalar_one()
            ct = edge.relay_privkey_ciphertext
    return ct.decode("ascii") if isinstance(ct, (bytes, bytearray)) else ct


async def test_stale_message_rejected(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair
    # Build + sign a payload with an issued_at well outside the freshness window.
    priv = relay_keys.unwrap_private_key(await _edge_privkey(d["src_tenant"], d["src_edge"]))
    payload = relay_protocol.build_payload(
        ic_txn_id=uuid.uuid4(), edge_id=d["src_edge"],
        src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
        amount=Decimal("100.00"), entry_date=date(2026, 6, 6), description="stale",
        nonce=uuid.uuid4(),
        issued_at=datetime.now(UTC) - timedelta(seconds=settings.ic_relay_freshness_seconds + 600),
    )
    sig = relay_signing.sign(relay_signing.canonical_payload(payload), priv)
    env = {"payload": payload, "signature": b64encode(sig).decode("ascii")}
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 400 and "stale" in resp.text.lower()


async def test_wrong_token_rejected_401(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    env = _envelope_from_outbox(outbox)
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": "Bearer icrl_totally-wrong-token"},
        )
    assert resp.status_code == 401


async def test_routing_mismatch_rejected(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    """A body addressed to dst but delivered with a different X-Tenant-Id -> 400."""
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    env = _envelope_from_outbox(outbox)
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["src_tenant"]),  # WRONG tenant header
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 400


# ----------------------------------------------------------------------------- #
# Flag-off gating
# ----------------------------------------------------------------------------- #
async def test_flag_off_originator_post_raises(remote_pair: dict[str, Any]) -> None:
    # No relay_on fixture -> flag is its default (False).
    assert settings.ic_remote_relay_enabled is False
    d = remote_pair
    with pytest.raises(ic_svc.RemoteRelayDisabled):
        await _post_originator(d)
    # Nothing was written.
    async with AsyncSessionLocal() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM ic_outbox WHERE tenant_id = :t"),
            {"t": d["src_tenant"]})).scalar_one()
    assert n == 0


async def test_flag_off_accept_returns_503(remote_pair: dict[str, Any]) -> None:
    assert settings.ic_remote_relay_enabled is False
    d = remote_pair
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept",
            json={"payload": {"edge_id": str(d["dst_edge"]),
                              "dst_tenant_id": str(d["dst_tenant"]),
                              "ic_txn_id": str(uuid.uuid4()), "nonce": str(uuid.uuid4()),
                              "amount": "1.00", "entry_date": "2026-06-06",
                              "issued_at": "2026-06-06T00:00:00Z"},
                  "signature": ""},
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 503


# ----------------------------------------------------------------------------- #
# Cross-tenant write isolation — /ic/accept only writes the RECEIVER's tenant.
# ----------------------------------------------------------------------------- #
async def test_accept_only_writes_receiver_tenant(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    d = remote_pair
    _ic_txn, outbox = await _post_originator(d)
    env = _envelope_from_outbox(outbox)
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 200
    # The receiver's accept must NOT have created any ic_legs/ic_inbox in the
    # SRC tenant — only the originator's own outbox/leg (1 each) pre-exist there.
    async with AsyncSessionLocal() as s:
        src_inbox = (await s.execute(text(
            "SELECT count(*) FROM ic_inbox WHERE tenant_id = :t"),
            {"t": d["src_tenant"]})).scalar_one()
        src_legs = (await s.execute(text(
            "SELECT count(*) FROM ic_legs WHERE tenant_id = :t"),
            {"t": d["src_tenant"]})).scalar_one()
    assert src_inbox == 0, "accept leaked an inbox row into the SRC tenant"
    assert src_legs == 1, "SRC tenant should still have only its originator leg"


# ----------------------------------------------------------------------------- #
# Hardening regression (adversarial review): the freshness window is SHORT and
# enforced at the boundary. The old 24h default left a day-long window for a
# captured message to be re-injected before its nonce was first seen. The window
# is now ~10 min (configurable); a message just past it is rejected, and a fresh
# one is accepted with the SAME tight setting.
# ----------------------------------------------------------------------------- #
async def test_freshness_window_is_short_not_a_day(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    # The configured window must be tight (<= 15 min) — the hardening lever.
    assert settings.ic_relay_freshness_seconds <= 900, (
        f"freshness window {settings.ic_relay_freshness_seconds}s is too wide — "
        f"tighten to a short 5-15 min window (was 24h)"
    )


async def test_message_just_past_window_rejected(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    """A message issued just PAST the freshness window is rejected (post nothing)."""
    d = remote_pair
    priv = relay_keys.unwrap_private_key(await _edge_privkey(d["src_tenant"], d["src_edge"]))
    window = settings.ic_relay_freshness_seconds
    payload = relay_protocol.build_payload(
        ic_txn_id=uuid.uuid4(), edge_id=d["src_edge"],
        src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
        amount=Decimal("100.00"), entry_date=date(2026, 6, 6), description="edge-stale",
        nonce=uuid.uuid4(),
        # Just past the window (window + 30s) — proves the boundary, not just a
        # day-old message. With the old 24h default this would have been FRESH.
        issued_at=datetime.now(UTC) - timedelta(seconds=window + 30),
    )
    sig = relay_signing.sign(relay_signing.canonical_payload(payload), priv)
    env = {"payload": payload, "signature": b64encode(sig).decode("ascii")}
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 400 and "stale" in resp.text.lower(), resp.text
    async with AsyncSessionLocal() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM ic_inbox WHERE tenant_id = :t"),
            {"t": d["dst_tenant"]})).scalar_one()
    assert n == 0, "a just-past-window message must post nothing"


async def test_message_within_window_accepted(
    relay_on: None, remote_pair: dict[str, Any]
) -> None:
    """A fresh message (well within the tight window) is still accepted — the tighten
    must not break the happy path."""
    d = remote_pair
    priv = relay_keys.unwrap_private_key(await _edge_privkey(d["src_tenant"], d["src_edge"]))
    payload = relay_protocol.build_payload(
        ic_txn_id=uuid.uuid4(), edge_id=d["src_edge"],
        src_tenant_id=d["src_tenant"], dst_tenant_id=d["dst_tenant"],
        amount=Decimal("100.00"), entry_date=date(2026, 6, 6), description="fresh",
        nonce=uuid.uuid4(),
        issued_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    sig = relay_signing.sign(relay_signing.canonical_payload(payload), priv)
    env = {"payload": payload, "signature": b64encode(sig).decode("ascii")}
    from saebooks.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/intercompany/accept", json=env,
            headers={"X-Tenant-Id": str(d["dst_tenant"]),
                     "Authorization": f"Bearer {d['dst_token']}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("duplicate") is False
