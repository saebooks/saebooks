"""Per-company SISS credentials (Batch II)

Adds four nullable columns to ``companies`` so an Enterprise install
running as a MyData-as-Vendor service aggregator can hold one SISS CDR
client per tenant rather than one global set in env vars:

* ``siss_client_id`` — public-ish OAuth client id for the company's CDR
  registration. Plaintext; knowing it alone doesn't authenticate.
* ``siss_client_secret_encrypted`` — ciphertext produced by
  ``saebooks.services.crypto.encrypt_field``. Requires
  ``SAEBOOKS_FIELD_ENCRYPTION_KEY`` set. We store it ``text`` rather than
  ``varchar(N)`` because Fernet tokens are variable-length; capping would
  be a ghost footgun if the secret ever needs rotation to a different
  algorithm.
* ``siss_subscription_key_encrypted`` — same treatment for the APIM
  ``Ocp-Apim-Subscription-Key`` header value.
* ``siss_environment`` — free-text label ``'production'`` / ``'sandbox'``.
  Future-proofing for when SISS ships distinct endpoints per tier; the
  current resolver falls back to the global env-configured URLs.

No DB check constraint on ``siss_environment`` — we want to tolerate a
new value (``'staging'``, ``'pilot'``) without a schema bump. Validation
lives in the Python resolver.

Additive: NULL on every column means "fall back to env-var credentials"
(the pre-Batch-II behaviour), so upgrading doesn't change any install.

Revision ID: 0034_company_siss_creds
Revises: 0033_permissions
Create Date: 2026-04-22
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_company_siss_creds"
down_revision: str | None = "0033_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("siss_client_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("siss_client_secret_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("siss_subscription_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("siss_environment", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "siss_environment")
    op.drop_column("companies", "siss_subscription_key_encrypted")
    op.drop_column("companies", "siss_client_secret_encrypted")
    op.drop_column("companies", "siss_client_id")
