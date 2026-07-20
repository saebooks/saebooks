"""0202_money_numeric_18_4 — widen every money column to Numeric(18, 4).

M1.5 slice 5-PRIMITIVES: money storage moves from ``Numeric(18, 2)`` to
``Numeric(18, 4)`` so sub-cent ISO-4217 minor units (three-decimal
dinars, four-decimal unit costs) fit without a second schema pass.
Value-preserving: every existing 2-decimal amount is exactly
representable at 4 decimals, and the ORM ``Money`` type
(``saebooks.db_types``) trims the storage padding on read so AU
serialization stays byte-identical.

Also a merge revision: 0201_merge_ee_night_heads (EE night chains) and
0201_company_jurisdictions (SubJuris slice) both advanced off
0200_merge_einvoice_heads in parallel, leaving two heads that break the
harness's ``alembic upgrade head``. Both are additive with no ordering
dependency, so this revision joins them and applies the widening.

Revision ID: 0202_money_numeric_18_4
Revises: 0201_merge_ee_night_heads, 0201_company_jurisdictions
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0202_money_numeric_18_4"
down_revision: tuple[str, str] = (
    "0201_merge_ee_night_heads",
    "0201_company_jurisdictions",
)
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# 0172 relocated the pre-accounting tables out of ``public``; the alembic
# connection's search_path does not include that schema, so their ALTERs
# must be schema-qualified. (No capture-schema table carries money.)
_TABLE_SCHEMA: dict[str, str] = {
    "quotes": "preaccounting",
    "quote_lines": "preaccounting",
    "purchase_orders": "preaccounting",
    "purchase_order_lines": "preaccounting",
}

# Every ORM money column (saebooks/models/*.py mapped_column(Money())).
# Generated from the model definitions — keep the two in sync.
_MONEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("accounts", "credit_limit"),
    ("bills", "subtotal"),
    ("bills", "tax_total"),
    ("bills", "total"),
    ("bills", "amount_paid"),
    ("bills", "base_subtotal"),
    ("bills", "base_tax_total"),
    ("bills", "base_total"),
    ("bills", "base_amount_paid"),
    ("bill_lines", "line_subtotal"),
    ("bill_lines", "line_tax"),
    ("bill_lines", "line_total"),
    ("budgets", "amount"),
    ("credit_notes", "subtotal"),
    ("credit_notes", "tax_total"),
    ("credit_notes", "total"),
    ("credit_notes", "amount_allocated"),
    ("credit_note_lines", "line_subtotal"),
    ("credit_note_lines", "line_tax"),
    ("credit_note_lines", "line_total"),
    ("expenses", "subtotal"),
    ("expenses", "tax_total"),
    ("expenses", "total"),
    ("expenses", "base_subtotal"),
    ("expenses", "base_tax_total"),
    ("expenses", "base_total"),
    ("expense_lines", "line_subtotal"),
    ("expense_lines", "line_tax"),
    ("expense_lines", "line_total"),
    ("fixed_assets", "cost"),
    ("fixed_assets", "residual_value"),
    ("fixed_assets", "disposal_proceeds"),
    ("invoices", "subtotal"),
    ("invoices", "tax_total"),
    ("invoices", "total"),
    ("invoices", "amount_paid"),
    ("invoices", "base_subtotal"),
    ("invoices", "base_tax_total"),
    ("invoices", "base_total"),
    ("invoices", "base_amount_paid"),
    ("invoice_lines", "line_subtotal"),
    ("invoice_lines", "line_tax"),
    ("invoice_lines", "line_total"),
    ("invoice_lines", "franking_credit_amount"),
    ("invoice_lines", "margin_acq_cost"),
    ("payments", "amount"),
    ("payments", "base_amount"),
    ("payment_allocations", "amount"),
    ("purchase_orders", "subtotal"),
    ("purchase_orders", "tax_total"),
    ("purchase_orders", "total"),
    ("purchase_orders", "base_subtotal"),
    ("purchase_orders", "base_tax_total"),
    ("purchase_orders", "base_total"),
    ("purchase_order_lines", "line_subtotal"),
    ("purchase_order_lines", "line_tax"),
    ("purchase_order_lines", "line_total"),
    ("quotes", "subtotal"),
    ("quotes", "tax_total"),
    ("quotes", "total"),
    ("quote_lines", "line_total"),
    ("receipts", "subtotal"),
    ("receipts", "tax_total"),
    ("receipts", "total"),
    ("receipt_lines", "amount"),
    ("receipt_lines", "tax_amount"),
    ("receipt_lines", "line_total"),
    ("supplier_credit_notes", "subtotal"),
    ("supplier_credit_notes", "tax_total"),
    ("supplier_credit_notes", "total"),
    ("supplier_credit_note_lines", "line_subtotal"),
    ("supplier_credit_note_lines", "line_tax"),
    ("supplier_credit_note_lines", "line_total"),
    ("supplier_statements", "opening_balance"),
    ("supplier_statements", "closing_balance"),
    ("supplier_statements", "our_ap_as_at"),
    ("supplier_statements", "balance_delta"),
    ("supplier_statement_lines", "amount"),
)


def upgrade() -> None:
    for table, column in _MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(18, 4),
            existing_type=sa.Numeric(18, 2),
            schema=_TABLE_SCHEMA.get(table),
        )


def downgrade() -> None:
    # Lossy only for values carrying non-zero sub-cent digits; AU data
    # is always 2-decimal so the AU downgrade is value-preserving.
    for table, column in _MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(18, 2),
            existing_type=sa.Numeric(18, 4),
            schema=_TABLE_SCHEMA.get(table),
        )
