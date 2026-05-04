"""Shared wizard-state helper — consumed by the imports and ATO SBR routers.

W3 (ato_sbr worker) reads this module to consume the Wizard class.

## Public API

::

    from saebooks.api.v1._wizard import Wizard

    # Start a new wizard session (returns the new wizard UUID).
    wizard_id: uuid.UUID = await Wizard.start(
        session,
        kind="bank_csv",
        initial_state={"step": 0, "account_id": None},
        ttl_seconds=3600,  # default 1 hour
    )

    # Apply a partial patch and retrieve merged state (idempotent merge).
    merged: dict = await Wizard.step(
        session,
        wizard_id,
        patch_state={"account_id": "some-uuid", "step": 1},
    )

    # Fetch current state without mutating (returns None if expired/missing).
    current: dict | None = await Wizard.get(session, wizard_id)

    # Housekeeping -- delete expired rows; returns count deleted.
    deleted: int = await Wizard.expire_old(session)

## Storage

All state lives in the ``wizard_state`` Postgres table (migration
``0089_wizard_state``).  RLS policy ``wizard_state_tenant_isolation``
ensures ``tenant_id = current_setting('app.current_tenant')::uuid``, so
a wizard created by tenant A is invisible to tenant B even if tenant B
knows the UUID.

The ``get_session`` dep issues ``SET LOCAL app.current_tenant`` at the
start of every transaction, so RLS fires automatically on every query
this helper makes -- no explicit tenant filtering is needed in the
helper methods.

## TTL / expiry

Every wizard row carries an ``expires_at`` timestamp.  Rows past their
TTL are logically expired -- ``get`` returns ``None``, ``step`` raises
``WizardExpiredError``.  ``expire_old`` physically deletes them; call
it from a scheduled endpoint or a background task (not per-request).

## Thread safety

Optimistic locking is intentionally *absent* on wizard state: each
wizard is a single-user, single-browser session.  The ``step`` method
does a direct ``UPDATE`` on the row, relying on Postgres row-level
locking to serialise concurrent updates from the same session (which
are rare in practice and self-correcting -- the user would see a stale
state on next poll and re-submit).

## Kinds (W2 scope)

``kind`` is an opaque string.  The imports router uses:

- ``"bank_csv"`` -- bank statement CSV wizard
- ``"bank_ofx"`` -- bank statement OFX wizard
- ``"coa"``      -- chart of accounts import wizard
- ``"qbo"``      -- QBO migration wizard (requires FLAG_QBO_IMPORT)

W3 (ATO SBR) adds its own kind (``"ato_sbr"``).  No enum is defined
here so either worker can extend without touching this module.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class WizardExpiredError(Exception):
    """Raised by ``Wizard.step`` when the wizard has expired."""


class WizardNotFoundError(Exception):
    """Raised by ``Wizard.step`` when the wizard UUID does not exist (or
    is invisible to the current tenant under RLS)."""


class Wizard:
    """Static helpers for wizard_state rows.

    Every method accepts an ``AsyncSession`` whose tenant binding is
    already set by the ``get_session`` dep (or by the test fixture that
    stamps ``session.info["tenant_id"]``).  The session must *not* be
    committed inside these helpers -- the caller (router) owns the
    transaction boundary.
    """

    @staticmethod
    async def start(
        session: AsyncSession,
        kind: str,
        initial_state: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> uuid.UUID:
        """Insert a new wizard row and return its UUID.

        Args:
            session: The request-scoped async session (tenant already bound).
            kind: Opaque wizard kind string (e.g. ``"bank_csv"``).
            initial_state: Starting JSONB state dict.
            ttl_seconds: Seconds until the wizard row expires.  Defaults
                to 3600 (1 hour).

        Returns:
            The new wizard's UUID.

        The row's ``tenant_id`` is read from the current Postgres session
        GUC (``current_setting('app.current_tenant')``), not passed
        explicitly, so RLS is the single source of truth.
        """
        import json as _json

        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        wizard_id = uuid.uuid4()

        await session.execute(
            text(
                """
                INSERT INTO wizard_state (id, tenant_id, kind, state, expires_at)
                VALUES (
                    :wid,
                    current_setting('app.current_tenant')::uuid,
                    :kind,
                    :state::jsonb,
                    :expires_at
                )
                """
            ).bindparams(
                wid=str(wizard_id),
                kind=kind,
                state=_json.dumps(initial_state),
                expires_at=expires_at,
            )
        )
        await session.flush()
        return wizard_id

    @staticmethod
    async def step(
        session: AsyncSession,
        wizard_id: uuid.UUID,
        patch_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge ``patch_state`` onto the existing state and return merged state.

        The merge uses Postgres ``||`` (JSONB concatenation) so top-level
        keys in ``patch_state`` overwrite their counterparts in the stored
        state while unmentioned keys are preserved.

        Args:
            session: The request-scoped async session.
            wizard_id: UUID of the wizard to update.
            patch_state: Partial state to merge in.

        Returns:
            The fully merged state dict after the update.

        Raises:
            WizardNotFoundError: Row missing (or invisible under RLS).
            WizardExpiredError: Row exists but ``expires_at`` is in the past.
        """
        import json as _json

        # First fetch to check existence and expiry.
        row = await session.execute(
            text(
                "SELECT state, expires_at FROM wizard_state WHERE id = :wid"
            ).bindparams(wid=str(wizard_id))
        )
        existing = row.first()
        if existing is None:
            raise WizardNotFoundError(str(wizard_id))
        expires_at = existing.expires_at
        # Make timezone-aware for comparison if naive.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            raise WizardExpiredError(str(wizard_id))

        patch_json = _json.dumps(patch_state)
        result = await session.execute(
            text(
                """
                UPDATE wizard_state
                   SET state = state || :patch::jsonb,
                       updated_at = now()
                 WHERE id = :wid
                   AND expires_at > now()
                RETURNING state
                """
            ).bindparams(wid=str(wizard_id), patch=patch_json)
        )
        updated_row = result.first()
        if updated_row is None:
            # Race: expired between our check and the UPDATE.
            raise WizardExpiredError(str(wizard_id))
        raw = updated_row[0]
        # asyncpg may return the JSONB column as a string or dict.
        if isinstance(raw, str):
            return _json.loads(raw)
        return dict(raw)

    @staticmethod
    async def get(
        session: AsyncSession,
        wizard_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return the current state, or ``None`` if missing or expired.

        Does not mutate the row.  RLS ensures cross-tenant isolation.
        """
        import json as _json

        row = await session.execute(
            text(
                """
                SELECT state, expires_at
                  FROM wizard_state
                 WHERE id = :wid
                   AND expires_at > now()
                """
            ).bindparams(wid=str(wizard_id))
        )
        result = row.first()
        if result is None:
            return None
        raw = result[0]
        if isinstance(raw, str):
            return _json.loads(raw)
        return dict(raw)

    @staticmethod
    async def expire_old(session: AsyncSession) -> int:
        """Delete all rows whose ``expires_at`` is in the past.

        Returns the number of rows deleted.  Intended for a scheduled
        housekeeping endpoint -- not called per-request.
        """
        result = await session.execute(
            text("DELETE FROM wizard_state WHERE expires_at <= now() RETURNING id")
        )
        rows = result.fetchall()
        await session.flush()
        return len(rows)


__all__ = ["Wizard", "WizardExpiredError", "WizardNotFoundError"]
