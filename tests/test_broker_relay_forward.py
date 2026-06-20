"""Phase 3c broker /ic/relay — freshness + accepted-new vs rejected-replay.

Hardening regression (adversarial review). The broker is the ORIGINATOR-side hop
that the dispatcher POSTs a signed envelope to. Two defects are pinned here:

1. Freshness at the first hop. A stale / far-future envelope is rejected by
   the broker BEFORE it forwards (mirrors /ic/accept), so a captured message
   cannot be re-injected through the broker outside a tight window.
2. Accepted-new vs rejected-replay. A genuine first delivery returns
   delivered=True, duplicate=False and forwards once; a REPLAY of the same
   (edge_id, nonce) returns duplicate=True, delivered=False and forwards
   NOTHING — so the dispatcher can never mark an outbox row delivered off a
   replay (the false-positive-delivery-ack defect).

Runs the broker FastAPI app in-process over ASGI with its DB pointed at an
ephemeral test DB (mirrors tests/db/test_broker_migration.py's ephemeral-DB
idiom) and the partner forward mocked, so no live partner stack is needed.

Postgres only.
"""
from __future__ import annotations

import os
import uuid
from base64 import b64encode
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from saebooks.config import settings as tenant_settings
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay import protocol as relay_protocol
from saebooks.services.ic_relay import signing as relay_signing

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

os.environ.setdefault("SAEBOOKS_ENV", "test")


def _admin_url(base_url: str, dbname: str) -> str:
    return make_url(base_url).set(database=dbname).render_as_string(hide_password=False)


def _run_broker_alembic(database_url: str, target: str) -> None:
    import saebooks_group.config as bcfg

    here = os.path.dirname(os.path.dirname(__file__))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "saebooks_group", "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    prev = bcfg.settings.database_url
    bcfg.settings.database_url = database_url
    try:
        command.upgrade(cfg, target)
    finally:
        bcfg.settings.database_url = prev


@pytest_asyncio.fixture
async def broker_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, object]]:
    """An ephemeral broker DB + the broker app bound to it, forwarding mocked.

    Registers ONE active pair (src/dst pubkeys + token hashes), flips forwarding
    ON, tightens the broker freshness window, and patches the partner forward to
    a stub that records calls and returns 200.
    """
    import asyncio

    base_url = tenant_settings.database_url
    tmp_db = f"sb_broker_fwd_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(_admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{tmp_db}"'))
    finally:
        await admin.dispose()
    tmp_url = _admin_url(base_url, tmp_db)
    await asyncio.to_thread(_run_broker_alembic, tmp_url, "head")

    import saebooks_group.app as broker_app_mod
    import saebooks_group.config as bcfg

    eng = create_async_engine(tmp_url, future=True)
    SessionLocal = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(broker_app_mod, "SessionLocal", SessionLocal)
    monkeypatch.setattr(bcfg.settings, "relay_forwarding_enabled", True, raising=False)
    monkeypatch.setattr(broker_app_mod.settings, "relay_forwarding_enabled", True, raising=False)
    monkeypatch.setattr(broker_app_mod.settings, "relay_freshness_seconds", 600, raising=False)

    forwarded: list[dict] = []

    class _Resp:
        status_code = 200
        text = '{"status": "POSTED", "duplicate": false}'

        def json(self) -> dict:
            return {"status": "POSTED", "duplicate": False}

    class _StubClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, url, json, headers):
            forwarded.append({"url": url, "json": json, "headers": headers})
            return _Resp()

    monkeypatch.setattr(broker_app_mod.httpx, "AsyncClient", _StubClient)
    monkeypatch.setattr(broker_app_mod, "_resolve_dst_token", lambda edge_id: "icrl_dst")

    src_priv, src_pub = relay_keys.new_signing_key()
    _dst_priv, dst_pub = relay_keys.new_signing_key()
    src_token, _src_prefix = relay_keys.generate_edge_token()

    edge_id = uuid.uuid4()
    src_tenant = uuid.uuid4()
    dst_tenant = uuid.uuid4()
    async with SessionLocal() as s, s.begin():
        await s.execute(
            sa.text(
                "INSERT INTO pair_registry (edge_id, src_tenant_id, dst_tenant_id, "
                "src_endpoint, dst_endpoint, src_pubkey, dst_pubkey, "
                "src_relay_token_hash, dst_relay_token_hash, status) VALUES "
                "(:e, :st, :dt, :se, :de, :sp, :dp, :sh, :dh, 'ACTIVE')"
            ),
            {
                "e": edge_id, "st": src_tenant, "dt": dst_tenant,
                "se": "http://src", "de": "http://dst",
                "sp": src_pub, "dp": dst_pub,
                "sh": relay_keys.hash_edge_token(src_token),
                "dh": relay_keys.hash_edge_token("icrl_dst"),
            },
        )

    yield {
        "edge_id": edge_id, "src_tenant": src_tenant, "dst_tenant": dst_tenant,
        "src_priv": src_priv, "src_token": src_token, "forwarded": forwarded,
    }

    await eng.dispose()
    admin = create_async_engine(_admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": tmp_db})
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{tmp_db}"'))
    finally:
        await admin.dispose()


def _signed_envelope(d: dict, *, issued_at: datetime, nonce: uuid.UUID,
                     ic_txn_id: uuid.UUID | None = None) -> dict:
    payload = relay_protocol.build_payload(
        ic_txn_id=ic_txn_id or uuid.uuid4(),
        edge_id=d["edge_id"],
        src_tenant_id=d["src_tenant"],
        dst_tenant_id=d["dst_tenant"],
        amount=Decimal("5000.00"),
        entry_date=datetime(2026, 6, 6).date(),
        description="broker probe",
        nonce=nonce,
        issued_at=issued_at,
    )
    sig = relay_signing.sign(relay_signing.canonical_payload(payload), d["src_priv"])
    return {"payload": payload, "signature": b64encode(sig).decode("ascii")}


async def _client() -> AsyncClient:
    from saebooks_group.app import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://broker")


async def test_broker_rejects_stale_before_forwarding(broker_app: dict) -> None:
    d = broker_app
    env = _signed_envelope(
        d, issued_at=datetime.now(UTC) - timedelta(seconds=630), nonce=uuid.uuid4()
    )
    async with await _client() as ac:
        resp = await ac.post(
            "/ic/relay", json=env,
            headers={"Authorization": f"Bearer {d['src_token']}"},
        )
    assert resp.status_code == 400, resp.text
    assert "freshness" in resp.text.lower()
    assert d["forwarded"] == [], "a stale message must NOT be forwarded to the partner"


async def test_broker_accepted_new_then_rejected_replay(broker_app: dict) -> None:
    d = broker_app
    nonce = uuid.uuid4()
    env = _signed_envelope(d, issued_at=datetime.now(UTC), nonce=nonce)
    async with await _client() as ac:
        r1 = await ac.post(
            "/ic/relay", json=env,
            headers={"Authorization": f"Bearer {d['src_token']}"},
        )
        r2 = await ac.post(
            "/ic/relay", json=env,
            headers={"Authorization": f"Bearer {d['src_token']}"},
        )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1.get("delivered") is True and b1.get("duplicate") is False, b1
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2.get("duplicate") is True and b2.get("delivered") is False, (
        "REPLAY at the broker returned an accepted-new ack — a replayed nonce "
        "must be a rejected-replay, never a genuine delivery"
    )
    assert len(d["forwarded"]) == 1, (
        f"replay forwarded to partner {len(d['forwarded'])} times (must stay 1)"
    )
