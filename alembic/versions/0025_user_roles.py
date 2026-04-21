"""User roles — per-user role for role-gated actions

Revision ID: 0025_user_roles
Revises: 0024_aba_bank_details
Create Date: 2026-04-21

Introduces a ``users`` table bound to the Authentik username that
arrives on the ``Remote-User`` header from Caddy's forward-auth proxy.
Community single-tenant install doesn't need OIDC client state here —
the SSO front door has already authenticated the request by the time
the header lands.

Design decisions:

* Single ``role`` varchar column on the user row (values constrained
  by a CHECK) rather than a full M2M role-permission matrix. YAGNI
  until we have a real use case — the role model can be promoted to
  a proper table without a schema break later (``user_roles`` M2M
  would swap the varchar for a JOIN).
* Users are keyed on ``username`` (unique) because that's what arrives
  on the header; the UUID PK is only for FKs from future audit / role
  change tables.
* ``archived_at`` soft-delete mirrors the Contact pattern — we never
  hard-delete an audit-trail-bearing row.
* ``last_seen_at`` updated by the middleware on each request so the
  admin UI can show "active in last 7 days" without query log scraping.

Roles (v1):

* ``admin`` — can do everything, can manage other users
* ``accountant`` — can do everything except user admin + company delete
* ``bookkeeper`` — create/post invoices, bills, payments; no void,
  no BAS lodge
* ``readonly`` — GET-only — the "accountant-read-only" role from the
  charter
* ``client`` — limited to their own company; view-only in v1, later
  can accept quotes / pay invoices
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_user_roles"
down_revision: str | None = "0024_aba_bank_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VALID_ROLES = ("admin", "accountant", "bookkeeper", "readonly", "client")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "role",
            sa.String(16),
            nullable=False,
            server_default="readonly",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint(
            "role IN ('" + "', '".join(VALID_ROLES) + "')",
            name="ck_users_role_valid",
        ),
    )
    op.create_index(
        "ix_users_username_active",
        "users",
        ["username"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_username_active", table_name="users")
    op.drop_table("users")
