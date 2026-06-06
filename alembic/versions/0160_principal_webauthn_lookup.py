"""0159 — SECURITY DEFINER credential lookup for principal WebAuthn login.

REVIEW BRANCH ONLY (``feat/accountant-login``). NOT merged, NOT deployed.
Renumber at merge if a lower number lands first.

Why this migration exists
-------------------------
Migration 0156 created ``principal_fido2_credentials`` (a *global*, non-RLS
table — a principal is not owned by a tenant). The principal LOGIN ceremony
(``saebooks.services.principal_webauthn``) needs to resolve a credential by
its ``credential_id`` *before* any session exists, then derive the owning
principal id FROM that resolved row — never from a client-supplied value.

That is the single most important security invariant of the whole feature
(see ``docs/security/accountant-principal.md`` §10): ``principal_id`` is
server-derived from the verified assertion. We get it from the credential
the assertion was signed with, looked up here.

This mirrors ``webauthn_lookup_credential(bytea)`` (migration 0135) used by
the ordinary user passkey login. Although ``principal_fido2_credentials`` is
not RLS'd today, we still resolve the row through a ``SECURITY DEFINER``
function so:

* the auth path has ONE audited entry point for the cross-context read,
* if the table is ever placed behind RLS or its grants tightened, the login
  path keeps working without a code change,
* ``search_path`` is pinned (``pg_catalog, public``) to defeat search-path
  hijack — identical hardening to the 0156 resolvers.

Safety
------
* The function takes a single ``bytea`` credential id. ``credential_id`` is a
  256+-bit cryptographically-random blob assigned by the authenticator; it is
  unguessable, so the lookup cannot be used to enumerate principals.
* The function returns only what the assertion-verification step needs
  (id, principal_id, public_key, sign_count). The signature is STILL verified
  against ``public_key`` before any session is minted — a row match alone
  proves nothing.
* No table is created or altered; no tenant-scoped surface is added, so the
  new-table RLS checklist does not apply. Reversible: ``downgrade`` drops the
  function only.

Revision ID: 0159_principal_webauthn_lookup
Revises: 0158_reclassifications
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0160_principal_webauthn_lookup"
down_revision: str | None = "0159_ic_remote_relay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "saebooks_app"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite (Cashbook) has no SECURITY DEFINER and no cross-tenant
        # principals — single physical device == single tenant. The lookup
        # is a Postgres-only product surface; nothing to create on SQLite.
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION principal_webauthn_lookup_credential(
            cred_id bytea
        )
        RETURNS TABLE (
            id uuid,
            principal_id uuid,
            public_key bytea,
            sign_count bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT c.id, c.principal_id, c.public_key, c.sign_count
            FROM principal_fido2_credentials c
            WHERE c.credential_id = cred_id
            LIMIT 1;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION principal_webauthn_lookup_credential(bytea) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION principal_webauthn_lookup_credential(bytea) "
        f"TO {_APP_ROLE}"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        "DROP FUNCTION IF EXISTS principal_webauthn_lookup_credential(bytea)"
    )
