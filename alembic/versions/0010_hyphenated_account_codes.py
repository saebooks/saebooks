"""Transform account codes to hyphenated format (e.g. 11110 → 1-1110)

Revision ID: 0010_hyphenated_codes
Revises: 0009_gst_system_accounts
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_hyphenated_codes"
down_revision: str | None = "0009_gst_system_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Fetch all account ranges, longest prefix first (for correct matching)
    ranges = conn.execute(
        sa.text(
            "SELECT prefix, label, account_types, company_id "
            "FROM account_ranges ORDER BY length(prefix) DESC"
        )
    ).fetchall()

    # 2. Fetch all accounts
    accounts = conn.execute(
        sa.text("SELECT id, code FROM accounts")
    ).fetchall()

    # 3. Transform each account code: insert hyphen after matching prefix
    #    Handle bustard codes: "11234-a" (prefix "1") → "1-1234-a"
    import re
    bustard_re = re.compile(r"^(\d+)-([a-zA-Z])$")

    for acct_id, code in accounts:
        # Check if code already has a hyphen (bustard suffix)
        bm = bustard_re.match(code)
        if bm:
            digits, letter = bm.group(1), bm.group(2)
        else:
            digits, letter = code, ""

        for prefix, _label, _types, _cid in ranges:
            if digits.startswith(prefix):
                children = digits[len(prefix):]
                new_code = f"{prefix}-{children}"
                if letter:
                    new_code += f"-{letter}"
                conn.execute(
                    sa.text("UPDATE accounts SET code = :new_code WHERE id = :id"),
                    {"new_code": new_code, "id": acct_id},
                )
                break

    # 4. Update GST settings that reference account codes
    #    Settings.value is JSONB — store as JSON string literal
    gst_settings = {
        "gst_collected_account_code": "2-1310",
        "gst_paid_account_code": "2-1330",
        "gst_clearing_account_code": "2-1320",
    }
    import json
    for key, new_val in gst_settings.items():
        json_val = json.dumps(new_val)  # produces '"2-1310"'
        conn.execute(
            sa.text(
                "UPDATE settings SET value = CAST(:jval AS jsonb) WHERE key = :key"
            ),
            {"jval": json_val, "key": key},
        )

    # 5. Create top-level header accounts for each range
    #    Use the first active company if range doesn't supply one
    first_company_id = conn.execute(
        sa.text("SELECT id FROM companies WHERE archived_at IS NULL ORDER BY created_at LIMIT 1")
    ).scalar()

    for prefix, label, account_types, company_id in ranges:
        cid = company_id or first_company_id
        header_code = f"{prefix}-0000"

        # Skip if already exists
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM accounts "
                "WHERE company_id = :cid AND code = :code"
            ),
            {"cid": cid, "code": header_code},
        ).scalar()
        if exists:
            continue

        # First account_type from the range's array
        acct_type = account_types[0] if account_types else "ASSET"

        conn.execute(
            sa.text(
                "INSERT INTO accounts (company_id, code, name, account_type, is_header) "
                "VALUES (:cid, :code, :name, CAST(:acct_type AS account_type_enum), true)"
            ),
            {
                "cid": cid,
                "code": header_code,
                "name": label,
                "acct_type": acct_type,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()

    # 1. Fetch all account ranges (needed for prefix stripping and header deletion)
    ranges = conn.execute(
        sa.text(
            "SELECT prefix, label, account_types, company_id "
            "FROM account_ranges ORDER BY length(prefix) DESC"
        )
    ).fetchall()

    # 2. Delete header accounts created by upgrade
    first_company_id = conn.execute(
        sa.text("SELECT id FROM companies WHERE archived_at IS NULL ORDER BY created_at LIMIT 1")
    ).scalar()

    for prefix, _label, _types, company_id in ranges:
        cid = company_id or first_company_id
        header_code = f"{prefix}-0000"
        conn.execute(
            sa.text(
                "DELETE FROM accounts WHERE company_id = :cid AND code = :code AND is_header = true"
            ),
            {"cid": cid, "code": header_code},
        )

    # 3. Restore GST settings to old unhyphenated codes
    gst_settings = {
        "gst_collected_account_code": "21310",
        "gst_paid_account_code": "21330",
        "gst_clearing_account_code": "21320",
    }
    import json
    for key, old_val in gst_settings.items():
        json_val = json.dumps(old_val)
        conn.execute(
            sa.text(
                "UPDATE settings SET value = CAST(:jval AS jsonb) WHERE key = :key"
            ),
            {"jval": json_val, "key": key},
        )

    # 4. Remove hyphens from all account codes that match a range prefix
    accounts = conn.execute(
        sa.text("SELECT id, code FROM accounts")
    ).fetchall()

    for acct_id, code in accounts:
        for prefix, _label, _types, _cid in ranges:
            expected_prefix = f"{prefix}-"
            if code.startswith(expected_prefix):
                rest = code[len(expected_prefix):]
                old_code = f"{prefix}{rest}"
                conn.execute(
                    sa.text("UPDATE accounts SET code = :old_code WHERE id = :id"),
                    {"old_code": old_code, "id": acct_id},
                )
                break
