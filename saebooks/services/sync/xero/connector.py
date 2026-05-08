"""Top-level Xero sync orchestrator.

The single public entry point — ``sync_xero(session, connection)`` —
runs one full pull/push cycle against a single Xero org. It is called
by:

* The router's ``POST /api/v1/sync/xero/trigger`` endpoint (operator-
  initiated); fan-out to background task.
* The worker's scheduled poll (per plan §11.b "Cadence: every 15 min").

Each invocation:

1. Decrypts the OAuth refresh token from
   ``connection.oauth_refresh_token_ciphertext``.
2. Builds a ``XeroTokenCache`` whose ``on_refresh_rotated`` callback
   re-encrypts and persists the new refresh token under the same
   transaction that wraps the sync run.
3. Builds a ``XeroClient`` bound to ``connection.external_tenant_id``.
4. Runs ``pull_contacts``, then ``pull_invoices`` for ACCREC and
   ACCPAY, then ``push_contacts``, then ``push_invoices``.
5. Updates ``connection.last_pulled_at`` / ``last_pushed_at``.
6. Appends a per-run summary to ``sync_audit_log``.

If any step raises ``SyncAuthError`` we mark the connection
``revoked`` (refresh token dead) and stop. ``SyncRateLimited`` is
re-raised to the caller — the worker re-schedules the run after the
``Retry-After`` window.

Tenancy
-------
The session passed in MUST already have ``app.current_tenant`` set to
the connection's tenant — the caller (worker / router) is responsible.
We guard with a ``current_setting`` check at the top of the run.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.company import Company
from saebooks.models.sync import (
    SyncConnection,
    SyncConnectionStatus,
    SyncDirection,
    SyncProvider,
)
from saebooks.services.crypto import decrypt_field, encrypt_field
from saebooks.services.sync.errors import (
    SyncAuthError,
    SyncError,
    SyncNotConfiguredError,
)
from saebooks.services.sync.xero.client import XeroClient
from saebooks.services.sync.xero.pull import (
    PullStats,
    _audit,
    pull_contacts,
    pull_invoices,
)
from saebooks.services.sync.xero.push import (
    PushStats,
    push_contacts,
    push_invoices,
)
from saebooks.services.sync.xero.token import XeroTokenCache

log = logging.getLogger(__name__)


@dataclass
class SyncRunReport:
    """Aggregate result from one ``sync_xero`` run."""

    connection_id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime
    contacts_pull: PullStats
    invoices_pull_accrec: PullStats
    invoices_pull_accpay: PullStats
    contacts_push: PushStats
    invoices_push: PushStats
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": str(self.connection_id),
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "contacts_pull": self.contacts_pull.__dict__,
            "invoices_pull_accrec": self.invoices_pull_accrec.__dict__,
            "invoices_pull_accpay": self.invoices_pull_accpay.__dict__,
            "contacts_push": self.contacts_push.__dict__,
            "invoices_push": self.invoices_push.__dict__,
            "error": self.error,
        }


async def sync_xero(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    company_id: uuid.UUID | None = None,
    client_id_override: str | None = None,
    client_secret_override: str | None = None,
) -> SyncRunReport:
    """Run one full Xero sync cycle for ``connection``.

    Caller MUST hold a row lock on ``sync_connections`` (the worker
    takes ``SELECT ... FOR UPDATE`` before calling this) so two runs
    can't race on the same refresh token.

    ``company_id`` defaults to the tenant's first ``Company`` (sorted
    by ``created_at``) — Enterprise tenants typically have one company
    per tenant. The operator picks the mapping at consent time and we
    persist it on ``connection`` (TODO: add ``connection.company_id``
    column when multi-company-per-org sync becomes a requirement).

    The optional client_id/secret overrides are for tests — production
    decrypts from the connection row.
    """
    started_at = datetime.now(UTC)

    if connection.provider != SyncProvider.XERO.value:
        raise SyncError(
            f"sync_xero called with provider={connection.provider!r}; "
            "expected 'xero'"
        )
    if connection.status not in {
        SyncConnectionStatus.ACTIVE.value,
        SyncConnectionStatus.ERROR.value,
    }:
        raise SyncError(
            f"connection {connection.id} is {connection.status!r}; "
            "cannot sync"
        )
    if not connection.external_tenant_id:
        raise SyncNotConfiguredError(
            f"connection {connection.id} has no external_tenant_id; "
            "consent flow not complete"
        )

    # Sanity: tenant GUC must match the connection's tenant.
    current = await session.execute(text("SHOW app.current_tenant"))
    current_tenant_raw = current.scalar()
    if current_tenant_raw and current_tenant_raw != str(connection.tenant_id):
        raise SyncError(
            f"app.current_tenant ({current_tenant_raw!r}) does not match "
            f"connection.tenant_id ({connection.tenant_id!r})"
        )

    # Resolve company_id — first company on the tenant.
    if company_id is None:
        stmt = (
            select(Company)
            .where(Company.tenant_id == connection.tenant_id)
            .order_by(Company.created_at.asc())
            .limit(1)
        )
        company = (await session.execute(stmt)).scalar_one_or_none()
        if company is None:
            raise SyncError(
                f"tenant {connection.tenant_id} has no Company rows; "
                "create at least one before connecting Xero"
            )
        company_id = company.id

    # Build OAuth credentials.
    if client_id_override is not None:
        client_id = client_id_override
    else:
        if connection.oauth_client_id_ciphertext is None:
            raise SyncNotConfiguredError(
                f"connection {connection.id} missing OAuth client_id"
            )
        client_id = decrypt_field(
            connection.oauth_client_id_ciphertext.decode("ascii")
        )
    if client_secret_override is not None:
        client_secret = client_secret_override
    else:
        if connection.oauth_client_secret_ciphertext is None:
            raise SyncNotConfiguredError(
                f"connection {connection.id} missing OAuth client_secret"
            )
        client_secret = decrypt_field(
            connection.oauth_client_secret_ciphertext.decode("ascii")
        )
    if connection.oauth_refresh_token_ciphertext is None:
        raise SyncNotConfiguredError(
            f"connection {connection.id} missing OAuth refresh_token"
        )
    refresh_token = decrypt_field(
        connection.oauth_refresh_token_ciphertext.decode("ascii")
    )

    # Persist rotated refresh token on the connection row. Caller's
    # transaction wraps this; commit at the end of sync_xero.
    async def on_refresh_rotated(new_refresh: str) -> None:
        connection.oauth_refresh_token_ciphertext = encrypt_field(
            new_refresh
        ).encode("ascii")

    token_cache = XeroTokenCache(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        on_refresh_rotated=on_refresh_rotated,
    )
    client = XeroClient(
        token_cache=token_cache,
        xero_tenant_id=connection.external_tenant_id,
    )

    # Empty stats for early-return paths.
    empty_pull = PullStats()
    empty_push = PushStats()
    error_msg: str | None = None
    final_status = SyncConnectionStatus.ACTIVE.value
    contacts_pull = empty_pull
    accrec_pull = empty_pull
    accpay_pull = empty_pull
    contacts_push = empty_push
    invoices_push = empty_push

    try:
        # 1. Pull contacts (must precede invoices — invoice→contact link
        #    depends on contacts being in sync_state).
        contacts_pull = await pull_contacts(
            session,
            connection=connection,
            client=client,
            company_id=company_id,
        )
        # 2. Pull invoices: AR first (ACCREC), then AP (ACCPAY).
        accrec_pull = await pull_invoices(
            session,
            connection=connection,
            client=client,
            company_id=company_id,
            invoice_type="ACCREC",
        )
        accpay_pull = await pull_invoices(
            session,
            connection=connection,
            client=client,
            company_id=company_id,
            invoice_type="ACCPAY",
        )
        # 3. Push contacts.
        contacts_push = await push_contacts(
            session,
            connection=connection,
            client=client,
        )
        # 4. Push invoices (POSTED only).
        invoices_push = await push_invoices(
            session,
            connection=connection,
            client=client,
        )

        connection.last_pushed_at = datetime.now(UTC)
        # last_pulled_at advanced inside pull_* on success.
        connection.last_error = None
        connection.status = SyncConnectionStatus.ACTIVE.value
    except SyncAuthError as exc:
        # Refresh token dead -> connection revoked. Operator must
        # re-OAuth.
        connection.status = SyncConnectionStatus.REVOKED.value
        connection.last_error = str(exc)
        final_status = SyncConnectionStatus.REVOKED.value
        error_msg = str(exc)
        log.warning(
            "xero sync auth-error connection=%s: %s",
            connection.id,
            exc,
        )
    except SyncError as exc:
        connection.status = SyncConnectionStatus.ERROR.value
        connection.last_error = str(exc)
        final_status = SyncConnectionStatus.ERROR.value
        error_msg = str(exc)
        log.exception("xero sync failed connection=%s", connection.id)
    finally:
        await client.aclose()
        await token_cache.aclose()

    finished_at = datetime.now(UTC)

    report = SyncRunReport(
        connection_id=connection.id,
        status=final_status,
        started_at=started_at,
        finished_at=finished_at,
        contacts_pull=contacts_pull,
        invoices_pull_accrec=accrec_pull,
        invoices_pull_accpay=accpay_pull,
        contacts_push=contacts_push,
        invoices_push=invoices_push,
        error=error_msg,
    )

    # Per-run summary — useful for the operator's "sync history" view
    # without iterating every per-row row.
    await _audit(
        session,
        connection=connection,
        direction=SyncDirection.PULL if error_msg is None else SyncDirection.CONFLICT,
        object_type=None,
        external_id=None,
        outcome="ok" if error_msg is None else "error",
        message=error_msg or "sync run complete",
        payload=report.to_dict(),
    )
    return report


__all__ = ["SyncRunReport", "sync_xero"]
