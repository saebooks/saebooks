"""0149_cashbook_tax_code_mapping — make cashbook supplies visible to BAS (C1).

Three idempotent steps:

1. **Seed reporting_type fixes.** The AU starter seed mis-tagged two
   codes so G10/G2 could never be fed:
     - ``CAP`` ("GST on capital acquisitions") was ``reporting_type='taxable'``
       -> should be ``'capital'`` (BAS G10).
     - ``EXP`` ("Export (GST Free)") was ``reporting_type='gst_free'``
       -> should be ``'export'`` (BAS G2).
   We flip them only where they currently hold the wrong value, so a
   tenant who already corrected them by hand is untouched.

2. **Ensure capital + input-taxed codes exist.** Every company that has
   the GST baseline (an active ``GST`` code) gets an active ``CAP``
   (capital) and ``INP`` (input_taxed) code if missing -- these back the
   ``CAP_PURCHASE`` (G10) and ``EXP_BANK`` / ``INC_INTEREST``
   (input-taxed) cashbook categories. Idempotent via the partial unique
   index ``uq_tax_codes_company_code_active``.

3. **Backfill cashbook journal lines.** Cashbook-originated
   ``journal_lines`` were posted with NULL ``tax_code_id`` (the C1 bug),
   so they were invisible to the BAS aggregator. We stamp each
   cashbook JE's *category* line (the non-bank, non-GST-system line)
   with the tax_code its category maps to, resolved per company. The
   category->code map is embedded as a snapshot so this migration does
   not depend on app code that may drift.

Revision ID: 0149_cashbook_tax_code_mapping
Revises: 0148_contact_is_one_off
Create Date: 2026-06-04
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0149_cashbook_tax_code_mapping"
down_revision: str | None = "0148_contact_is_one_off"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# category_code -> (tax_code, reporting_type) -- snapshot of the v1
# cashbook category tax mapping (services/cashbook_categories.py). NULL
# categories (PER_DRAWINGS, TX_TRANSFER) are intentionally absent: their
# lines stay NULL because they are not BAS-reportable.
_CATEGORY_TAX_MAP: dict[str, tuple[str, str]] = {
    "INC_SALES": ("GST", "taxable"),
    "INC_SERVICES": ("GST", "taxable"),
    "INC_INTEREST": ("INP", "input_taxed"),
    "INC_OTHER": ("GST", "taxable"),
    "EXP_VEHICLE": ("GST", "taxable"),
    "EXP_HOME_OFFICE": ("GST", "taxable"),
    "EXP_INSURANCE": ("GST", "taxable"),
    "EXP_PROFESSIONAL": ("GST", "taxable"),
    "EXP_MATERIALS": ("GST", "taxable"),
    "EXP_SOFTWARE": ("GST", "taxable"),
    "EXP_TELCO": ("GST", "taxable"),
    "EXP_SUPER": ("FRE", "gst_free"),
    "EXP_TRAINING": ("GST", "taxable"),
    "EXP_TOOLS": ("GST", "taxable"),
    "EXP_TRAVEL": ("GST", "taxable"),
    "EXP_BANK": ("INP", "input_taxed"),
    "EXP_OTHER": ("GST", "taxable"),
    "CAP_PURCHASE": ("CAP", "capital"),
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- Step 1: fix mis-tagged seed reporting_types (where still wrong) ---
    conn.execute(
        sa.text(
            "UPDATE tax_codes SET reporting_type = 'capital' "
            "WHERE code = 'CAP' AND reporting_type = 'taxable' "
            "AND archived_at IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE tax_codes SET reporting_type = 'export' "
            "WHERE code = 'EXP' AND reporting_type = 'gst_free' "
            "AND archived_at IS NULL"
        )
    )

    # --- Step 2: ensure CAP (capital) + INP (input_taxed) per company ---
    # Insert for every company that has an active GST code but is missing
    # the target code. tenant_id/version copied from the company's GST row
    # to satisfy the tenant-coherence trigger. Partial unique index makes
    # this a no-op on re-run.
    for code, name, rate, rep in (
        ("CAP", "GST on capital acquisitions", "10.000", "capital"),
        ("INP", "Input Taxed", "0.000", "input_taxed"),
    ):
        conn.execute(
            sa.text(
                """
                INSERT INTO tax_codes
                    (id, company_id, tenant_id, code, name, rate,
                     tax_system, reporting_type, description, version,
                     created_at)
                SELECT gen_random_uuid(), g.company_id, g.tenant_id,
                       CAST(:code AS varchar), CAST(:name AS varchar),
                       CAST(:rate AS numeric),
                       'GST', CAST(:rep AS varchar), NULL, 1, now()
                FROM tax_codes g
                WHERE g.code = 'GST' AND g.archived_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM tax_codes x
                      WHERE x.company_id = g.company_id
                        AND x.code = CAST(:code AS varchar)
                        AND x.archived_at IS NULL
                  )
                """
            ),
            {"code": code, "name": name, "rate": rate, "rep": rep},
        )

    # --- Step 3: backfill cashbook-origin journal lines with NULL tax_code ---
    # For each cashbook JE (attachments->'cashbook_meta' present), find the
    # category line: same entry, NULL tax_code_id, NOT a GST system-managed
    # account, and on the side that matches the entry direction (income =>
    # credit line; expense => debit line) which excludes the bank
    # counter-line. Resolve the per-company TaxCode by the category's code,
    # falling back to reporting_type.
    for cat_code, (tc_code, rep_type) in _CATEGORY_TAX_MAP.items():
        conn.execute(
            sa.text(
                """
                WITH cb AS (
                    SELECT je.id AS entry_id,
                           je.company_id,
                           je.attachments->'cashbook_meta'->>'direction' AS direction
                    FROM journal_entries je
                    WHERE je.attachments->'cashbook_meta' IS NOT NULL
                      AND je.attachments->'cashbook_meta'->>'category_code' = :cat
                ),
                tgt AS (
                    SELECT jl.id AS line_id, cb.company_id
                    FROM journal_lines jl
                    JOIN cb ON cb.entry_id = jl.entry_id
                    JOIN accounts a ON a.id = jl.account_id
                    WHERE jl.tax_code_id IS NULL
                      AND COALESCE(a.system_managed, false) = false
                      AND (
                           (cb.direction = 'income'  AND jl.credit > 0)
                        OR (cb.direction = 'expense' AND jl.debit  > 0)
                      )
                ),
                resolved AS (
                    SELECT tgt.line_id,
                           COALESCE(
                             (SELECT t.id FROM tax_codes t
                              WHERE t.company_id = tgt.company_id
                                AND t.code = :tc
                                AND t.archived_at IS NULL
                              LIMIT 1),
                             (SELECT t.id FROM tax_codes t
                              WHERE t.company_id = tgt.company_id
                                AND t.reporting_type = :rep
                                AND t.archived_at IS NULL
                              ORDER BY t.code
                              LIMIT 1)
                           ) AS tc_id
                    FROM tgt
                )
                UPDATE journal_lines jl
                SET tax_code_id = resolved.tc_id
                FROM resolved
                WHERE jl.id = resolved.line_id
                  AND resolved.tc_id IS NOT NULL
                """
            ),
            {"cat": cat_code, "tc": tc_code, "rep": rep_type},
        )


def downgrade() -> None:
    # Irreversible by design for the backfill (we did not snapshot the
    # prior NULLs per line). The reporting_type fixes and code inserts are
    # left in place: reverting them would re-break BAS. A no-op downgrade
    # is the honest, safe choice -- re-running upgrade() is idempotent.
    pass
