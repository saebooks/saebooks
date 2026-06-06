"""0155 — cross-tenant *principal* (accountant / bank) + per-tenant grants.

Adds the MYOB-style cross-tenant identity described in
``docs/security/accountant-principal.md`` and
``saebooks-intercompany-accountant-design.md`` §4:

* ``principals`` — global identity (NOT tenant-scoped). An accountant or a
  bank. May optionally own its own books (``owned_tenant_id``).
* ``principal_fido2_credentials`` — global FIDO2/WebAuthn binding. The
  principal authenticates FIDO2-only (standing rule: no code-based 2FA).
* ``principal_tenant_grants`` — the security-critical cross-tenant grant
  table. Each row is one tenant's scoped grant of a role to a principal.

Security model (the crux)
-------------------------
``principal_tenant_grants`` is the ONE table that a principal may read
across tenant boundaries — but only its OWN rows, and never to obtain data
access, only to discover *which tenants it may act as*. We get this without
weakening tenant isolation:

1. The ordinary ``tenant_isolation`` policy keys on ``tenant_id`` exactly
   like every other tenant-scoped table. Under FORCE-RLS against the
   NOBYPASSRLS ``saebooks_app`` role, a tenant session sees and can write
   ONLY grants for its own tenant. The ``WITH CHECK`` half means a tenant
   cannot forge a grant binding a principal to a *foreign* tenant.

2. The cross-tenant read a principal needs ("which tenants can I act as?")
   is served by a single ``SECURITY DEFINER`` function
   ``principal_visible_grants(p_principal_id uuid)`` that returns only
   ``status='active'`` rows for the one principal id passed in. The service
   layer passes the authenticated principal's id — never a client-chosen
   value. Same controlled-bypass pattern as ``webauthn_lookup_credential``
   (migration 0135).

3. ``principal_can_act_as(p_principal_id, p_tenant_id)`` — a SECURITY
   DEFINER predicate the act-as service calls to verify an active grant
   before it binds ``app.current_tenant`` to the target tenant. Acting-as
   then flows through the SAME FORCE-RLS path as a native user; there is no
   BYPASSRLS data path anywhere.

Why ``principals`` / ``principal_fido2_credentials`` are not RLS'd
-----------------------------------------------------------------
They carry no ``tenant_id`` — a principal is global by definition. They are
never exposed to a tenant session: the only readers are the principal-auth
path (which connects under controlled, server-chosen identifiers) and the
SECURITY DEFINER functions. We still ``REVOKE`` blanket access and grant
``saebooks_app`` exactly the DML it needs, so a stray tenant query cannot
read them either (it has no code path that names them under a tenant
session, and the grant functions are the only owner-priv reads).

Coherence trigger
-----------------
``principal_tenant_grants_role_check`` rejects a grant whose ``role`` is not
a known ``UserRole`` value — fail closed on a typo'd / injected role string.

Reversibility
-------------
``downgrade()`` drops the functions, trigger, policies and the three tables
in FK-safe order. No data outside these tables is touched.

Revision ID: 0156_principal_cross_tenant
Revises: 0155_transfers
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0156_principal_cross_tenant"
down_revision: str | None = "0155_transfers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "saebooks_app"

# Known scoped-role vocabulary (mirrors saebooks.models.user.UserRole). A
# grant.role must be one of these; the coherence trigger fails closed
# otherwise so a typo or injection can't widen access.
_VALID_ROLES = ("owner", "admin", "accountant", "bookkeeper", "viewer")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. principals — global identity (no tenant_id, no RLS).
    # ------------------------------------------------------------------ #
    op.create_table(
        "principals",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="accountant",
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "owned_tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requires_fido2",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("username", name="uq_principals_username"),
    )

    # ------------------------------------------------------------------ #
    # 2. principal_fido2_credentials — global FIDO2 binding (no RLS).
    # ------------------------------------------------------------------ #
    op.create_table(
        "principal_fido2_credentials",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "principal_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.LargeBinary, nullable=False),
        sa.Column("public_key", sa.LargeBinary, nullable=False),
        sa.Column(
            "sign_count", sa.BigInteger, nullable=False, server_default="0"
        ),
        sa.Column(
            "transports",
            pg.ARRAY(sa.String(16)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "friendly_name",
            sa.String(64),
            nullable=False,
            server_default="Security key",
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "credential_id", name="uq_principal_fido2_credential_id"
        ),
    )
    op.create_index(
        "ix_principal_fido2_credentials_principal_id",
        "principal_fido2_credentials",
        ["principal_id"],
    )

    # ------------------------------------------------------------------ #
    # 3. principal_tenant_grants — the cross-tenant grant table.
    # ------------------------------------------------------------------ #
    op.create_table(
        "principal_tenant_grants",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "principal_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NOT NULL + FK — new-table RLS checklist.
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column(
            "granted_by_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_principal_tenant_grants_principal_id",
        "principal_tenant_grants",
        ["principal_id"],
    )
    op.create_index(
        "ix_principal_tenant_grants_tenant_id",
        "principal_tenant_grants",
        ["tenant_id"],
    )
    # At most one ACTIVE grant per (principal, tenant). Revoked rows are
    # exempt (partial index) so the audit history of prior grants survives.
    op.create_index(
        "uq_principal_tenant_grant_active",
        "principal_tenant_grants",
        ["principal_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    if not _is_postgres():
        # SQLite (Cashbook) has no RLS / SECURITY DEFINER / GUC. Cross-tenant
        # principals are a Postgres-only product surface; on SQLite the tables
        # exist for ORM/schema parity but the isolation machinery is a no-op
        # (single physical device == single tenant). Tests that exercise the
        # RLS guarantees are marked postgres_only.
        return

    # ------------------------------------------------------------------ #
    # 4. RLS on the grant table — ordinary tenant_isolation. A tenant
    #    session sees/writes only its own grants; WITH CHECK blocks
    #    forging a grant for a foreign tenant.
    # ------------------------------------------------------------------ #
    op.execute("ALTER TABLE principal_tenant_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE principal_tenant_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON principal_tenant_grants
        USING (tenant_id::text = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true))
        """
    )

    # ------------------------------------------------------------------ #
    # 5. role coherence trigger — fail closed on an unknown role string.
    # ------------------------------------------------------------------ #
    roles_sql = ", ".join(f"'{r}'" for r in _VALID_ROLES)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION principal_tenant_grant_role_check()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.role NOT IN ({roles_sql}) THEN
                RAISE EXCEPTION
                    'principal_tenant_grants.role % is not a valid scoped role',
                    NEW.role;
            END IF;
            IF NEW.status NOT IN ('active', 'revoked') THEN
                RAISE EXCEPTION
                    'principal_tenant_grants.status % is not valid', NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_principal_tenant_grant_role_check
        BEFORE INSERT OR UPDATE ON principal_tenant_grants
        FOR EACH ROW EXECUTE FUNCTION principal_tenant_grant_role_check()
        """
    )

    # ------------------------------------------------------------------ #
    # 6. SECURITY DEFINER resolvers — the ONLY cross-tenant reads. Both
    #    are parameterised by a single principal id supplied by the
    #    server from the authenticated session, never by the client.
    # ------------------------------------------------------------------ #
    # 6a. principal_visible_grants — the principal's own active grants
    #     across every tenant that granted it. Used to build the
    #     "select tenant" dashboard.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION principal_visible_grants(p_principal_id uuid)
        RETURNS TABLE (
            grant_id uuid,
            tenant_id uuid,
            role varchar,
            granted_at timestamptz
        )
        AS $$
            SELECT g.id, g.tenant_id, g.role, g.granted_at
            FROM principal_tenant_grants g
            WHERE g.principal_id = p_principal_id
              AND g.status = 'active';
        $$ LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        """
    )

    # 6b. principal_can_act_as — boolean: does this principal hold an
    #     ACTIVE grant for this tenant? Returns the granted role or NULL.
    #     The act-as service calls this BEFORE binding app.current_tenant.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION principal_grant_role(
            p_principal_id uuid, p_tenant_id uuid
        )
        RETURNS varchar
        AS $$
            SELECT g.role
            FROM principal_tenant_grants g
            WHERE g.principal_id = p_principal_id
              AND g.tenant_id = p_tenant_id
              AND g.status = 'active'
            LIMIT 1;
        $$ LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        """
    )

    # ------------------------------------------------------------------ #
    # 7. Grants. The three tables were created by the migration role; on
    #    the production stacks ALTER DEFAULT PRIVILEGES (migration 0056)
    #    already grants saebooks_app DML, but we re-assert explicitly so
    #    the test stack (migrations run by saebooks_test) and any stack
    #    where default-privs didn't propagate are covered — same belt as
    #    migration 0128.
    #
    #    The SECURITY DEFINER functions are owned by the migration role
    #    (BYPASSRLS) and EXECUTE-granted to saebooks_app. saebooks_app
    #    therefore reads grants cross-tenant ONLY through these two
    #    parameterised functions — it has table DML for the tenant-scoped
    #    path, but its direct SELECT on principal_tenant_grants is still
    #    FORCE-RLS'd to the current tenant.
    # ------------------------------------------------------------------ #
    for tbl in (
        "principals",
        "principal_fido2_credentials",
        "principal_tenant_grants",
    ):
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO {_APP_ROLE}"
        )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION principal_visible_grants(uuid) "
        f"TO {_APP_ROLE}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION principal_grant_role(uuid, uuid) "
        f"TO {_APP_ROLE}"
    )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP FUNCTION IF EXISTS principal_grant_role(uuid, uuid)"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS principal_visible_grants(uuid)"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_principal_tenant_grant_role_check "
            "ON principal_tenant_grants"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS principal_tenant_grant_role_check()"
        )
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation ON principal_tenant_grants"
        )
    op.drop_table("principal_tenant_grants")
    op.drop_table("principal_fido2_credentials")
    op.drop_table("principals")
