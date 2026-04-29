"""Public-auth scaffolding: per-user auth tokens + rate-limit counters.

Adds the columns the public signup / verify / reset / magic-link
endpoints need on ``users``:

* ``email_verified_at``, ``email_verification_token_hash``,
  ``email_verification_expires_at`` — verification flow
* ``password_reset_token_hash``, ``password_reset_expires_at`` — reset flow
* ``magic_link_token_hash``, ``magic_link_expires_at`` — magic-link login
* ``password_version`` — bumped on password rotation; checked by
  ``require_bearer`` so old JWTs are invalidated globally on reset

All ``*_token_hash`` columns are SHA-256 hex (CHAR(64)) with a partial
index ``WHERE NOT NULL`` so token-lookup is O(1) without bloating the
index when the columns are usually NULL.

Also creates ``rate_limit_counters`` — a simple Postgres-backed
fixed-window counter keyed by ``(scope_key, window_start)``. No RLS;
this is pre-auth infra and the table is not multi-tenant.

Revision ID: 0077_user_auth_tokens
Revises: 0076_audit_log
Create Date: 2026-04-29
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0077_user_auth_tokens"
down_revision: str | None = "0076_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TOKEN_COLS = (
    ("email_verification_token_hash", "ix_users_email_verification_token_hash"),
    ("password_reset_token_hash", "ix_users_password_reset_token_hash"),
    ("magic_link_token_hash", "ix_users_magic_link_token_hash"),
)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_verification_token_hash", sa.CHAR(64), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_verification_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "users",
        sa.Column("password_reset_token_hash", sa.CHAR(64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "password_reset_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column("magic_link_token_hash", sa.CHAR(64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "magic_link_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "password_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    for col, idx in _TOKEN_COLS:
        op.create_index(
            idx,
            "users",
            [col],
            unique=False,
            postgresql_where=sa.text(f"{col} IS NOT NULL"),
        )

    op.create_table(
        "rate_limit_counters",
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint(
            "scope_key", "window_start", name="pk_rate_limit_counters"
        ),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_counters")
    for _col, idx in _TOKEN_COLS:
        op.drop_index(idx, table_name="users")
    op.drop_column("users", "password_version")
    op.drop_column("users", "magic_link_expires_at")
    op.drop_column("users", "magic_link_token_hash")
    op.drop_column("users", "password_reset_expires_at")
    op.drop_column("users", "password_reset_token_hash")
    op.drop_column("users", "email_verification_expires_at")
    op.drop_column("users", "email_verification_token_hash")
    op.drop_column("users", "email_verified_at")
