"""0135_user_webauthn_credentials — per-user WebAuthn / FIDO2 credentials table.

Stores public keys + metadata for hardware security keys (YubiKey),
platform authenticators (Touch ID, Windows Hello), and passkeys enrolled
on a user's account.

Schema:
  user_webauthn_credentials(id, tenant_id, user_id, credential_id, public_key,
                            sign_count, transports, aaguid, friendly_name,
                            last_used_at, created_at)
    — tenant-scoped, FORCE RLS + tenant_isolation policy.

Discoverable-credential login flow (server doesn't know who you are until
after WebAuthn assertion) needs to look up a credential by credential_id
WITHOUT a tenant context. Solved via SECURITY DEFINER lookup function
``webauthn_lookup_credential(bytea)`` that bypasses RLS for this one
authenticated read path.

The previous (0081_oauth_and_fido2) migration added fido2_registered_at /
fido2_credential_count columns to users; those are kept and now sourced
from this table via DB triggers.

Revision ID: 0135_user_webauthn_credentials
Revises: 0134_branches
Create Date: 2026-05-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0135_user_webauthn_credentials"
down_revision: str | None = "0134_branches"
branch_labels: Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. table
    op.create_table(
        "user_webauthn_credentials",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "user_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        # WebAuthn credential ID (binary). Returned by the authenticator at
        # registration; presented again at every authentication. Unique
        # across all users globally (cryptographically guaranteed by the
        # authenticator).
        sa.Column("credential_id", sa.LargeBinary, nullable=False),
        # COSE-encoded public key (binary). Used to verify the signature
        # on every authentication assertion.
        sa.Column("public_key", sa.LargeBinary, nullable=False),
        # Anti-replay counter. Authenticator increments on every use; we
        # store the last-seen value and reject assertions with a lower or
        # equal count.
        sa.Column("sign_count", sa.BigInteger, nullable=False, server_default="0"),
        # Transports the authenticator advertised at registration time:
        # 'usb' / 'nfc' / 'ble' / 'internal' / 'hybrid'. Stored as text[]
        # so we can show appropriate prompts ("insert your USB security
        # key" vs "use Touch ID").
        sa.Column(
            "transports", pg.ARRAY(sa.String(16)),
            nullable=False, server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        # 16-byte authenticator model id. Lets us identify the make/model
        # of the key (e.g. YubiKey 5 NFC). Optional but useful for display.
        sa.Column("aaguid", sa.LargeBinary, nullable=False, server_default=sa.text("'\\x00000000000000000000000000000000'::bytea")),
        sa.Column("friendly_name", sa.String(64), nullable=False, server_default="Security key"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("credential_id", name="uq_user_webauthn_credentials_credential_id"),
    )
    op.create_index(
        "ix_user_webauthn_credentials_user_id",
        "user_webauthn_credentials", ["user_id"],
    )
    op.create_index(
        "ix_user_webauthn_credentials_tenant_id",
        "user_webauthn_credentials", ["tenant_id"],
    )

    # 2. RLS — tenant isolation. The discoverable-credential login path
    # bypasses this via the SECURITY DEFINER lookup below.
    op.execute("ALTER TABLE user_webauthn_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_webauthn_credentials FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON user_webauthn_credentials
        USING (tenant_id::text = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true))
    """)

    # 3. Tenant coherence: user_webauthn_credentials.tenant_id MUST match
    # users.tenant_id for the same user_id. Prevents a credential being
    # attributed to a user in a different tenant via API mis-routing.
    op.execute("""
        CREATE OR REPLACE FUNCTION user_webauthn_credentials_tenant_coherence()
        RETURNS trigger AS $$
        DECLARE
            u_tenant uuid;
        BEGIN
            SELECT tenant_id INTO u_tenant FROM users WHERE id = NEW.user_id;
            IF u_tenant IS NULL THEN
                RAISE EXCEPTION 'user_webauthn_credentials.user_id (%) not found in users', NEW.user_id;
            END IF;
            IF u_tenant <> NEW.tenant_id THEN
                RAISE EXCEPTION 'user_webauthn_credentials.tenant_id (%) does not match users.tenant_id (%) for user_id %',
                    NEW.tenant_id, u_tenant, NEW.user_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_user_webauthn_credentials_tenant_coherence
        BEFORE INSERT OR UPDATE OF tenant_id, user_id ON user_webauthn_credentials
        FOR EACH ROW EXECUTE FUNCTION user_webauthn_credentials_tenant_coherence()
    """)

    # 4. SECURITY DEFINER function for discoverable-credential login.
    # When a user authenticates with a passkey (no prior session), the
    # server receives a credential_id from the browser. We need to look
    # up that credential to find the user and verify the signature, but
    # we don't have a tenant context yet. This function runs as the table
    # owner (BYPASSRLS) and returns just enough to bootstrap the session.
    #
    # Returns (user_id, tenant_id, public_key, sign_count) or empty set.
    # Called by the API layer with the credential_id parsed from the
    # WebAuthn assertion. Safe because:
    #   - credential_id is cryptographically unguessable (256+ bits)
    #   - we still verify the signature before minting a session
    #   - the function returns only what's needed for verification
    op.execute("""
        CREATE OR REPLACE FUNCTION webauthn_lookup_credential(cred_id bytea)
        RETURNS TABLE (
            id uuid,
            user_id uuid,
            tenant_id uuid,
            public_key bytea,
            sign_count bigint
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT c.id, c.user_id, c.tenant_id, c.public_key, c.sign_count
            FROM user_webauthn_credentials c
            WHERE c.credential_id = cred_id
            LIMIT 1
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION webauthn_lookup_credential(bytea) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION webauthn_lookup_credential(bytea) TO PUBLIC")

    # 5. Trigger to keep users.fido2_registered_at / fido2_credential_count
    # in sync with the credentials table. (These columns were added in
    # 0081 but never properly maintained; this fixes that.)
    op.execute("""
        CREATE OR REPLACE FUNCTION user_webauthn_credentials_sync_user_counters()
        RETURNS trigger AS $$
        DECLARE
            target_user uuid;
            new_count integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                target_user := OLD.user_id;
            ELSE
                target_user := NEW.user_id;
            END IF;
            SELECT count(*) INTO new_count FROM user_webauthn_credentials
            WHERE user_id = target_user;
            UPDATE users
            SET fido2_credential_count = new_count,
                fido2_registered_at = COALESCE(fido2_registered_at,
                                               CASE WHEN new_count > 0 THEN now() END)
            WHERE id = target_user;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_user_webauthn_credentials_sync_counters
        AFTER INSERT OR DELETE ON user_webauthn_credentials
        FOR EACH ROW EXECUTE FUNCTION user_webauthn_credentials_sync_user_counters()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_user_webauthn_credentials_sync_counters ON user_webauthn_credentials")
    op.execute("DROP FUNCTION IF EXISTS user_webauthn_credentials_sync_user_counters()")
    op.execute("DROP FUNCTION IF EXISTS webauthn_lookup_credential(bytea)")
    op.execute("DROP TRIGGER IF EXISTS trg_user_webauthn_credentials_tenant_coherence ON user_webauthn_credentials")
    op.execute("DROP FUNCTION IF EXISTS user_webauthn_credentials_tenant_coherence()")
    op.drop_index("ix_user_webauthn_credentials_tenant_id", "user_webauthn_credentials")
    op.drop_index("ix_user_webauthn_credentials_user_id", "user_webauthn_credentials")
    op.drop_table("user_webauthn_credentials")
