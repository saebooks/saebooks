"""0219_tpar_bde_fields — columns the TPAR BDE flat file needs.

The ATO ruled (DSPPT-49560, 2026-07-17) that an in-house product lodges
TPAR via the BDE/OS4B file-transfer channel, whose FPAIVV03.0 payee
record carries fields the ledger never captured:

* ``contacts`` gains a person-name split (family/given/other given).
  The flat file REQUIRES surname + first given name for individual
  payees (business name blank) — a single ``name`` string can't say
  which shape a contact is. Jurisdiction-neutral: name parts are just
  as useful to the EE module, so they live on core contacts.
* ``tpar_lines`` gains the remaining DPAIVS fields snapshot-copied at
  run build time: tax withheld (zero-valid, mandatory in the file),
  statement-by-supplier + amendment indicators, the name split, and
  the optional phone/email/BSB/account details.

All columns are additive with defaults — existing rows and the
aggregation SQL keep working unchanged.

Revision ID: 0219_tpar_bde_fields
Revises: 0218_sync_state_tables
Create Date: 2026-07-17
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0219_tpar_bde_fields"
down_revision: str | None = "0218_sync_state_tables"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CONTACT_COLUMNS = (
    ("family_name", "varchar(64)"),
    ("given_name", "varchar(64)"),
    ("other_given_name", "varchar(64)"),
)

_TPAR_LINE_COLUMNS = (
    ("tax_withheld", "numeric(18,2) NOT NULL DEFAULT 0"),
    ("statement_by_supplier", "boolean NOT NULL DEFAULT false"),
    ("amendment", "boolean NOT NULL DEFAULT false"),
    ("payee_family_name", "varchar(64)"),
    ("payee_given_name", "varchar(64)"),
    ("payee_other_given_name", "varchar(64)"),
    ("payee_phone", "varchar(32)"),
    ("payee_email", "varchar(128)"),
    ("payee_bsb", "varchar(16)"),
    ("payee_account_number", "varchar(16)"),
)


def upgrade() -> None:
    for name, ddl in _CONTACT_COLUMNS:
        op.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {name} {ddl};")
    for name, ddl in _TPAR_LINE_COLUMNS:
        op.execute(f"ALTER TABLE tpar_lines ADD COLUMN IF NOT EXISTS {name} {ddl};")


def downgrade() -> None:
    for name, _ in _TPAR_LINE_COLUMNS:
        op.execute(f"ALTER TABLE tpar_lines DROP COLUMN IF EXISTS {name};")
    for name, _ in _CONTACT_COLUMNS:
        op.execute(f"ALTER TABLE contacts DROP COLUMN IF EXISTS {name};")
