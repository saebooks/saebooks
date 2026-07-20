"""tax_returns — FILED status + filed_at timestamp (Packet 4c).

Why this migration exists
--------------------------
The generic ``/lodge`` endpoint (``api/v1/tax_returns.py``) dispatches to
the SBR/ATO ``LodgementService`` rail (BAS/IAS/STP/TPAR/SuperStream) — it
422s on any other ``return_type``, including EE's KMD/TSD. EE's live
X-Road KMD3 rail (``EELodgementAdapter``) is a separate, credential-gated
async submit→poll→confirm state machine (see 0196's ``ee_filing_*``
columns) that isn't provisioned yet.

What's missing in between is the plain manual "file-and-confirm" case: an
accountant generates the KMD/TSD file from the engine, files it themselves
(e.g. via EMTA's e-service portal) outside any automated rail, and the
app needs a simple, honest way to record "this return has been filed" —
a status transition with a timestamp, independent of which rail (if any)
was used to file it.

Two additive columns:

  * ``tax_return_status`` enum gains a ``'filed'`` value (``ALTER TYPE
    ... ADD VALUE`` — safe inside this migration's transaction because
    the new value isn't used until a later statement/migration; Postgres
    forbids using a value added earlier in the SAME transaction, and this
    migration adds it and stops).
  * ``tax_returns.filed_at`` — nullable ``TIMESTAMPTZ``, NULL for every
    existing row and every row never manually filed through this path.

No backfill, fully additive. AU behaviour is untouched: AU returns
continue to go through ``/lodge`` -> status ``'lodged'``, never
``'filed'``, so no existing AU row or test is affected.

Revision ID: 0199_tax_return_filed_status
Revises: 0198_company_control_accounts
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0199_tax_return_filed_status"
down_revision: str | None = "0198_company_control_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE tax_return_status ADD VALUE IF NOT EXISTS 'filed'")
    op.add_column(
        "tax_returns",
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE — the enum value is left
    # in place on downgrade (harmless: simply unused once the column and
    # the API endpoint that could set it are gone). Only the column drops.
    op.drop_column("tax_returns", "filed_at")
