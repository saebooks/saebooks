"""ATO SBR Machine Credential onboarding (Batch II.5)

Adds ``ato_sbr_configs`` — one row per ``Company`` that has onboarded
a RAM Machine Credential + Software Service ID (SSID) for STP / BAS
e-lodgement. Separate from ``companies`` because:

* the credential rotates (RAM machine credentials expire) — a dedicated
  row keeps rotation history tidy,
* multiple onboarding-related metadata columns (issuer CN, not_before,
  not_after, last EVTE ping) would bloat ``companies`` too far,
* a future DSP install may need to tag whether this tenant is
  self-lodger or DSP-mode.

All fields nullable; absence of a row means "onboarding not started".
The encrypted blob + password columns are ``Text`` rather than
``LargeBinary`` because ``services.crypto.encrypt_field`` returns a
base64 Fernet token (string) — keeping them text means the same
encrypt/decrypt helpers as the Batch II SISS credentials.

Revision ID: 0035_ato_sbr_config
Revises: 0034_company_siss_creds
Create Date: 2026-04-22
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_ato_sbr_config"
down_revision: str | None = "0034_company_siss_creds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ato_sbr_configs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            server_default="self_lodger",
        ),
        sa.Column(
            "environment",
            sa.String(16),
            nullable=False,
            server_default="evte",
        ),
        sa.Column("keystore_encrypted", sa.Text(), nullable=True),
        sa.Column("keystore_password_encrypted", sa.Text(), nullable=True),
        sa.Column("keystore_filename", sa.String(255), nullable=True),
        sa.Column("keystore_subject_cn", sa.String(255), nullable=True),
        sa.Column("keystore_issuer_cn", sa.String(255), nullable=True),
        sa.Column("keystore_serial", sa.String(128), nullable=True),
        sa.Column("keystore_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keystore_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ssid", sa.String(64), nullable=True),
        sa.Column(
            "mygovid_confirmed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "ram_authority_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "downloader_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "evte_verified_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "prod_verified_at", sa.DateTime(timezone=True), nullable=True
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
    )


def downgrade() -> None:
    op.drop_table("ato_sbr_configs")
