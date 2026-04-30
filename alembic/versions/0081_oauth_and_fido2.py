"""Add OAuth2 and FIDO2 authentication fields.

Migration adds:
1. oauth_provider_links table for linking OAuth identities to users
2. fido2_registered_at and fido2_credential_count to users table

Revision ID: 0081_oauth_and_fido2
Revises: 0080_contact_messages
Create Date: 2026-04-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0081_oauth_and_fido2"
down_revision: str | None = "0080_contact_messages"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add OAuth and FIDO2 support."""
    # Create oauth_provider_links table
    op.create_table(
        "oauth_provider_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=False),
        sa.Column("provider_user_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(("user_id",), ("users.id",), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_provider"),
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_provider_user_id"),
    )

    # Add FIDO2 fields to users table
    op.add_column("users", sa.Column("fido2_registered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("fido2_credential_count", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    """Remove OAuth and FIDO2 support."""
    # Remove FIDO2 fields from users table
    op.drop_column("users", "fido2_credential_count")
    op.drop_column("users", "fido2_registered_at")

    # Drop oauth_provider_links table
    op.drop_table("oauth_provider_links")
