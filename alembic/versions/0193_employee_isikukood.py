"""employees.isikukood_encrypted (kmd-inf-tsd scope Packet 4).

The TSD Lisa-1 per-person row key (scope §2.2/§3.2: "isikukood is (a)
the Lisa-1 row key and (b) sensitive PII (encodes DOB + sex) — it must
not sit as plaintext in a JSONB blob"). Additive, nullable ``Text``,
Fernet ciphertext via ``saebooks.services.crypto`` — mirrors
``employees.tfn_encrypted`` (``0110_employees_and_super_funds``)
EXACTLY: same column type, same encrypt-at-write/decrypt-at-read
access pattern (``services.employees._encrypt_opt``/``_decrypt_opt``),
same "plaintext never touches the column, service layer holds it only
briefly between request and insert" discipline. The scope explicitly
rejected the ``Employee.extra`` JSONB dodge for this field.

Nullable -> no server_default, no backfill. Fully reversible via
``op.drop_column``. Chains from the current company-DB head,
``0192_ee_pensionable_age_flag`` (verified via
``grep -rl down_revision.*0192`` returning nothing before this file
was added — 0192 was still a leaf; NOTE this is 0193, not the scope's
originally-quoted 0191 — Packet 3 landed 0191/0192 first, chain from
what exists NOW per the build's migration discipline).

Revision ID: 0193_employee_isikukood
Revises:     0192_ee_pensionable_age_flag
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0193_employee_isikukood"
down_revision: str | None = "0192_ee_pensionable_age_flag"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "employees"
_COLUMN = "isikukood_encrypted"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Text(),
            nullable=True,
            comment=(
                "EE isikukood (personal identification code), Fernet "
                "ciphertext via services.crypto — mirrors "
                "tfn_encrypted's access pattern exactly. Plaintext "
                "never persisted. TSD Lisa-1 row key."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
