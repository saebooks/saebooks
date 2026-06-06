"""Outbox dispatcher for the IC REMOTE relay (Phase 3c) — lifespan task.

Drains ``ic_outbox`` PENDING/FAILED rows whose ``next_attempt_at`` is due, POSTs
the already-signed canonical payload to the broker ``/ic/relay`` with the
per-edge token, and advances the row's state machine with exponential backoff.
After ``ic_relay_max_attempts`` the row goes DEAD and surfaces in the recon view
for HUMAN action — it is NEVER auto-reversed (plan D5): the originator's local
leg is already final; a delivery failure must not mutate the books.

Gating
------
The dispatcher only starts when ``SAEBOOKS_IC_REMOTE_RELAY_ENABLED`` is True
(default OFF). With the flag off the task is never created (see main.lifespan),
so the outbox is inert.

No-BYPASSRLS data path
----------------------
The dispatcher enumerates tenant ids from the (non-tenant-scoped) ``tenants``
table via the pre-auth engine — metadata only, no GL, mirrors how /auth/login
resolves a user before a tenant is bound. The actual outbox + edge-key reads run
under the ``saebooks_app`` (NOBYPASSRLS) role with ``app.current_tenant`` bound
per tenant, so every money-adjacent read is FORCE-RLS-scoped. There is no
cross-tenant data path here.

Concurrency
-----------
The poll uses ``FOR UPDATE SKIP LOCKED`` so two dispatcher instances (or a
restart overlap) never double-send the same outbox row.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from base64 import b64encode
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.models.ic import IcEdge, IcOutbox, IcOutboxStatus
from saebooks.services.ic_relay.broker_client import (
    BrokerClient,
    BrokerError,
    BrokerRejected,
)

log = logging.getLogger("saebooks.services.ic_relay.dispatcher")

# Exponential backoff base (seconds): delay = base * 2**(attempts-1), capped.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600


def _next_backoff(attempts: int) -> datetime:
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), _BACKOFF_CAP_SECONDS)
    return datetime.now(UTC) + timedelta(seconds=delay)


async def _list_tenant_ids(login_session: AsyncSession) -> list[uuid.UUID]:
    """Enumerate tenant ids (metadata only) via the pre-auth engine.

    The tenants table is not tenant-scoped and holds no GL — this is the same
    posture as /auth/login resolving a user before a tenant GUC is bound.
    """
    rows = (await login_session.execute(text("SELECT id FROM tenants"))).all()
    return [r.id for r in rows]


async def _drain_tenant(
    app_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    broker_factory,
    settings: Settings,
    limit: int = 50,
) -> int:
    """Send up to ``limit`` due outbox rows for ONE bound tenant. Returns count.

    ``app_session`` MUST be the NOBYPASSRLS app-role session with
    ``app.current_tenant`` bound to ``tenant_id``. We claim rows with
    ``FOR UPDATE SKIP LOCKED`` so concurrent dispatchers don't collide.
    """
    now = datetime.now(UTC)
    claimed = (
        await app_session.execute(
            select(IcOutbox)
            .where(
                IcOutbox.tenant_id == tenant_id,
                IcOutbox.status.in_(
                    [IcOutboxStatus.PENDING, IcOutboxStatus.FAILED]
                ),
                (IcOutbox.next_attempt_at.is_(None))
                | (IcOutbox.next_attempt_at <= now),
            )
            .order_by(IcOutbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    sent = 0
    for row in claimed:
        edge = (
            await app_session.execute(
                select(IcEdge).where(
                    IcEdge.id == row.edge_id,
                    IcEdge.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if edge is None or edge.relay_token_prefix is None:
            row.status = IcOutboxStatus.FAILED
            row.attempts += 1
            row.last_error = "edge missing / not enabled for relay"
            row.next_attempt_at = _next_backoff(row.attempts)
            continue

        # The per-edge token cleartext is held by the BROKER's secret store; the
        # tenant stores only the hash. The dispatcher presents the token it was
        # issued at enable time — sourced from the runtime secret (env / vault).
        # For the loop test + until secret wiring lands, the token is resolved
        # via the injected broker_factory's bound token. Here we pass the
        # token prefix as a routing hint; the real cleartext is supplied by the
        # broker_factory closure (tests inject it; prod resolves from secrets).
        token = broker_factory.resolve_token(edge.id)
        if token is None:
            row.status = IcOutboxStatus.FAILED
            row.attempts += 1
            row.last_error = "no relay token available for edge"
            row.next_attempt_at = _next_backoff(row.attempts)
            continue

        client: BrokerClient = broker_factory.client()
        sig_b64 = b64encode(bytes(row.signature)).decode("ascii")
        try:
            await client.relay(
                payload=dict(row.payload_json),
                signature_b64=sig_b64,
                token=token,
            )
        except BrokerRejected as exc:
            # A 4xx that is not retryable (e.g. 400 bad sig) still backs off but
            # will keep failing until the edge is fixed; 5xx is transient.
            row.attempts += 1
            row.last_error = f"broker rejected: {exc}"
            if row.attempts >= settings.ic_relay_max_attempts:
                row.status = IcOutboxStatus.DEAD
            else:
                row.status = IcOutboxStatus.FAILED
                row.next_attempt_at = _next_backoff(row.attempts)
            continue
        except BrokerError as exc:
            row.attempts += 1
            row.last_error = f"broker unavailable: {exc}"
            if row.attempts >= settings.ic_relay_max_attempts:
                row.status = IcOutboxStatus.DEAD
            else:
                row.status = IcOutboxStatus.FAILED
                row.next_attempt_at = _next_backoff(row.attempts)
            continue

        # 2xx — the broker accepted and (in 3c) forwarded to the partner.
        row.status = IcOutboxStatus.ACKED
        row.attempts += 1
        row.last_error = None
        row.next_attempt_at = None
        sent += 1

    await app_session.commit()
    return sent


class _ProdBrokerFactory:
    """Default broker factory — one client to the configured broker URL.

    ``resolve_token`` reads per-edge tokens from the runtime secret store. Until
    that wiring lands (it is part of the go-live, not this review branch) it
    returns None, so the dispatcher backs a row off rather than sending an
    unauthenticated message. Tests inject their own factory.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def client(self) -> BrokerClient:
        return BrokerClient(base_url=self._settings.ic_broker_url)

    def resolve_token(self, edge_id: uuid.UUID) -> str | None:
        # Secret wiring is a go-live step (per-edge token vault lookup).
        return None


async def run_dispatcher_once(
    *,
    settings: Settings | None = None,
    broker_factory=None,
    app_session_factory=None,
    login_session_factory=None,
) -> int:
    """Run a single drain pass across all tenants. Returns total sent.

    Injectable factories make this unit-testable without the live engines:
    ``app_session_factory()`` yields a NOBYPASSRLS app-role AsyncSession,
    ``login_session_factory()`` yields the pre-auth session for tenant
    enumeration, and ``broker_factory`` supplies the broker client + token
    resolver. Production defaults wire the real engines.
    """
    cfg = settings if settings is not None else _default_settings
    if not cfg.ic_remote_relay_enabled:
        return 0

    if app_session_factory is None or login_session_factory is None:
        from saebooks.db import (
            AppSessionLocal,
            AsyncSessionLocal,
            LoginSessionLocal,
        )

        # Prefer the strict NOBYPASSRLS app-role sessionmaker; fall back to the
        # runtime sessionmaker (which is itself the app role at request time)
        # when the dedicated CLI app engine is unconfigured.
        app_session_factory = AppSessionLocal or AsyncSessionLocal
        login_session_factory = LoginSessionLocal
    bf = broker_factory or _ProdBrokerFactory(cfg)

    async with login_session_factory() as login_session:
        tenant_ids = await _list_tenant_ids(login_session)

    total = 0
    for tid in tenant_ids:
        async with app_session_factory() as app_session:
            app_session.info["tenant_id"] = str(tid)
            async with app_session.begin():
                await app_session.execute(
                    text(f"SET LOCAL app.current_tenant = '{tid}'")
                )
            total += await _drain_tenant(
                app_session,
                tenant_id=tid,
                broker_factory=bf,
                settings=cfg,
            )
    return total


async def dispatcher_loop(*, settings: Settings | None = None, stop: asyncio.Event | None = None) -> None:
    """The lifespan background task: poll → drain → sleep, until stopped.

    Only started by main.lifespan when the relay flag is ON. A crash in one pass
    is logged and the loop continues — a dispatcher fault must not bring the api
    down, and the outbox rows are durable so nothing is lost.
    """
    cfg = settings if settings is not None else _default_settings
    interval = max(cfg.ic_relay_poll_seconds, 0.5)
    log.info("ic-relay dispatcher started (interval=%.1fs)", interval)
    while stop is None or not stop.is_set():
        try:
            await run_dispatcher_once(settings=cfg)
        except Exception:  # pragma: no cover — never let the loop die
            log.exception("ic-relay dispatcher pass failed; continuing")
        try:
            if stop is not None:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            else:
                await asyncio.sleep(interval)
        except TimeoutError:
            pass
    log.info("ic-relay dispatcher stopped")
