"""Add intermediate header accounts for proper hierarchy grouping.

Inserts sub-headers like "Current Assets", "Cash & Bank", "Inventory",
"Property, Plant & Equipment" etc. so the account list isn't flat.

Revision ID: 0012_sub_headers
Revises: 0011_audit_snapshots
Create Date: 2026-04-16
"""
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_sub_headers"
down_revision: str | None = "0011_audit_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Sub-headers to insert: (code, name, account_type)
SUB_HEADERS = [
    # Assets sub-headers
    ("1-1000", "Current Assets", "ASSET"),
    ("1-1100", "Cash & Bank", "ASSET"),
    ("1-1200", "Receivables", "ASSET"),
    ("1-1300", "Inventory", "ASSET"),
    ("1-2000", "Prepayments & Deposits", "ASSET"),
    ("1-3000", "Property, Plant & Equipment", "ASSET"),
    # Liabilities sub-headers
    ("2-1000", "Current Liabilities", "LIABILITY"),
    ("2-2000", "Non-Current Liabilities", "LIABILITY"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Get the first active company
    row = conn.execute(
        sa.text(
            "SELECT id FROM companies WHERE archived_at IS NULL "
            "ORDER BY created_at LIMIT 1"
        )
    ).fetchone()
    if row is None:
        return  # no company — nothing to do
    company_id = row[0]

    for code, name, acct_type in SUB_HEADERS:
        # Skip if already exists
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM accounts WHERE company_id = :cid AND code = :code"
            ),
            {"cid": company_id, "code": code},
        ).fetchone()
        if exists:
            continue

        new_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO accounts (id, company_id, code, name, account_type, "
                "is_header, reconcile, system_managed) "
                "VALUES (:id, :cid, :code, :name, "
                "CAST(:acct_type AS account_type_enum), "
                "true, false, false)"
            ),
            {
                "id": new_id,
                "cid": company_id,
                "code": code,
                "name": name,
                "acct_type": acct_type,
            },
        )

    # Now fix parent_id for all accounts in this company
    # Walk every account and re-derive its parent using the zeroing-right approach
    all_accounts = conn.execute(
        sa.text(
            "SELECT id, code FROM accounts "
            "WHERE company_id = :cid AND archived_at IS NULL "
            "ORDER BY code"
        ),
        {"cid": company_id},
    ).fetchall()

    # Build code→id lookup
    code_to_id = {r[1]: r[0] for r in all_accounts}

    import re
    pat = re.compile(r"^(\d+)-(\d+)(?:-([a-zA-Z]))?$")

    for acct_id, code in all_accounts:
        m = pat.match(code)
        if not m:
            continue
        prefix = m.group(1)
        children = m.group(2)
        bustard = m.group(3) or ""

        parent_id = None

        # Bustard: parent is same code without bustard
        if bustard:
            candidate = f"{prefix}-{children}"
            if candidate in code_to_id and candidate != code:
                parent_id = code_to_id[candidate]
        else:
            # Walk children right-to-left, zeroing each position
            for i in range(len(children) - 1, -1, -1):
                candidate = f"{prefix}-{children[:i]}{'0' * (len(children) - i)}"
                if candidate != code and candidate in code_to_id:
                    parent_id = code_to_id[candidate]
                    break

        # Update parent_id
        conn.execute(
            sa.text("UPDATE accounts SET parent_id = :pid WHERE id = :aid"),
            {"pid": parent_id, "aid": acct_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    row = conn.execute(
        sa.text(
            "SELECT id FROM companies WHERE archived_at IS NULL "
            "ORDER BY created_at LIMIT 1"
        )
    ).fetchone()
    if row is None:
        return
    company_id = row[0]

    codes = [code for code, _, _ in SUB_HEADERS]
    for code in codes:
        conn.execute(
            sa.text(
                "DELETE FROM accounts WHERE company_id = :cid AND code = :code"
            ),
            {"cid": company_id, "code": code},
        )
