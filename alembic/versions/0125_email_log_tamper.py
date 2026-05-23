"""Phase D — tamper-evident email_send_log.

Once a row is inserted, it should be append-only EXCEPT for the post-send
delivery-event columns that the Resend webhook needs to update
(``delivered_at``, ``bounced_at``, ``bounce_reason``, ``opened_at``,
``opened_count``, ``clicked_at``, ``clicked_count``, ``complained_at``,
``webhook_events``).

Strategy: a BEFORE trigger that:

  * On UPDATE — compares OLD vs NEW for every column NOT in the
    delivery-event whitelist. Any change → RAISE EXCEPTION. Override
    by setting ``app.audit_force_update = 'true'`` (SET LOCAL only —
    superuser-only for emergency fixes).
  * On DELETE — always blocks. Override by setting
    ``app.audit_force_delete = 'true'``.

Why a trigger, not a role GRANT: the saebooks runtime role currently has
unrestricted privileges on every table (single-role deployment for now).
Splitting into a separate audit role would require ~50 GRANT changes
across the schema; a trigger gives us the same outcome with no role
shuffle, and it survives a future role-split unchanged.

Revision ID: 0125_email_log_tamper_evident
Revises: 0124_email_log_b_and_c
Create Date: 2026-05-23
"""
from alembic import op

revision: str = "0125_email_log_tamper"
down_revision: str | None = "0124_email_log_b_and_c"
branch_labels = None
depends_on = None


# Columns the Resend webhook + future delivery-event handlers may update.
# Adding a new delivery event? Add the column here too.
_WEBHOOK_UPDATABLE_COLUMNS = [
    "delivered_at",
    "bounced_at",
    "bounce_reason",
    "opened_at",
    "opened_count",
    "clicked_at",
    "clicked_count",
    "complained_at",
    "webhook_events",
]


def upgrade() -> None:
    cols_sql = ", ".join(f"'{c}'" for c in _WEBHOOK_UPDATABLE_COLUMNS)

    op.execute(f"""
        CREATE OR REPLACE FUNCTION email_send_log_block_update()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            allowed_cols TEXT[] := ARRAY[{cols_sql}];
            col TEXT;
            override TEXT;
        BEGIN
            -- Operator escape hatch — must be SET LOCAL'd in the same txn
            -- by a superuser before the UPDATE.
            override := current_setting('app.audit_force_update', true);
            IF override = 'true' THEN
                RETURN NEW;
            END IF;

            -- Walk every column on the table. If NEW differs from OLD on
            -- any non-whitelisted column, reject.
            FOR col IN
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'email_send_log'
            LOOP
                IF col = ANY(allowed_cols) THEN
                    CONTINUE;
                END IF;
                -- to_jsonb avoids the "<column> is not a known type at trigger
                -- creation time" issue and handles NULLs cleanly.
                IF to_jsonb(NEW)->col IS DISTINCT FROM to_jsonb(OLD)->col THEN
                    RAISE EXCEPTION
                        'email_send_log is tamper-evident: column "%" cannot be modified after insert (set app.audit_force_update=true to override)',
                        col
                        USING ERRCODE = 'check_violation';
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION email_send_log_block_delete()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            override TEXT;
        BEGIN
            override := current_setting('app.audit_force_delete', true);
            IF override = 'true' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'email_send_log is tamper-evident: DELETE blocked (set app.audit_force_delete=true to override)'
                USING ERRCODE = 'check_violation';
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER trg_email_send_log_block_update
        BEFORE UPDATE ON email_send_log
        FOR EACH ROW
        EXECUTE FUNCTION email_send_log_block_update();
    """)

    op.execute("""
        CREATE TRIGGER trg_email_send_log_block_delete
        BEFORE DELETE ON email_send_log
        FOR EACH ROW
        EXECUTE FUNCTION email_send_log_block_delete();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_email_send_log_block_delete ON email_send_log")
    op.execute("DROP TRIGGER IF EXISTS trg_email_send_log_block_update ON email_send_log")
    op.execute("DROP FUNCTION IF EXISTS email_send_log_block_delete()")
    op.execute("DROP FUNCTION IF EXISTS email_send_log_block_update()")
