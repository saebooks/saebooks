"""Foreign currency v1 — document currency + fx_rate on AR/AP + cached rate snapshots

Revision ID: 0027_foreign_currency
Revises: 0026_projects_budgets
Create Date: 2026-04-21

Scope (Batch GG/2, MVP):

Invoices, bills and payments can be *denominated* in a foreign currency.
Each header stores the ``currency`` code (3-letter ISO) and the
``fx_rate`` (to base currency) applied at issue/payment time. GL
posting still happens in base currency so the existing reports are
untouched — we just store the base-currency translation on the header
so the book of account doesn't need to re-derive it.

* ``invoices``/``bills``: add ``currency`` + ``fx_rate`` + four
  ``base_*`` shadow columns (``base_subtotal``, ``base_tax_total``,
  ``base_total``, ``base_amount_paid``). The ``base_amount_paid`` is
  maintained by the payment-allocation refresh path using the payment's
  own rate, so we can compute realised FX gain/loss per settlement.
* ``payments``: add ``currency`` + ``fx_rate`` + ``base_amount``.
* ``fx_rate_snapshots``: date-keyed cache of fetched rates — the
  reval / lookup service reads-through this before hitting the RBA
  (next batch: PP) to avoid pummelling the free endpoint and to make
  deterministic tests easy.

Realised FX on settlement (Dr/Cr ``6-1640 Exchange Rate Gain`` /
``6-1630 Exchange Rate Loss``) is computed in ``services/fx/`` at
settle time. Those two accounts already exist in the AU seed (see
``seed/au/account.account-au.csv``) — no CoA bump needed.

Unrealised (period-end) revaluation is explicitly deferred to
Batch PP. GG/2 is realised-only so the migration stays small and the
settle-path math is easy to verify in isolation.

Everything below is additive: every new column gets a safe default
(currency='AUD', fx_rate=1, base_*=total/amount_paid/amount). Existing
rows are backfilled in the ``upgrade`` so the invariant
``base_total == total`` holds for all legacy AUD-only data.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_foreign_currency"
down_revision: str | None = "0026_projects_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_document_fx_columns(table: str, has_amount_paid: bool) -> None:
    """Add (currency, fx_rate, base_subtotal, base_tax_total, base_total[, base_amount_paid])
    to a document-shape table (invoices, bills).
    """
    op.add_column(
        table,
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            server_default="AUD",
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "fx_rate",
            sa.Numeric(18, 8),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "base_subtotal",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "base_tax_total",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "base_total",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    if has_amount_paid:
        op.add_column(
            table,
            sa.Column(
                "base_amount_paid",
                sa.Numeric(18, 2),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # Backfill base_* = * for the existing legacy AUD rows. Safe because
    # the default on every new column is already correct for new rows.
    set_clause = (
        "base_subtotal = subtotal, "
        "base_tax_total = tax_total, "
        "base_total = total"
    )
    if has_amount_paid:
        set_clause += ", base_amount_paid = amount_paid"
    op.execute(f"UPDATE {table} SET {set_clause}")


def upgrade() -> None:
    # --- invoices / bills: header FX + base_* shadow columns --------------
    _add_document_fx_columns("invoices", has_amount_paid=True)
    _add_document_fx_columns("bills", has_amount_paid=True)

    # --- payments: header FX + base_amount shadow ------------------------
    op.add_column(
        "payments",
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            server_default="AUD",
        ),
    )
    op.add_column(
        "payments",
        sa.Column(
            "fx_rate",
            sa.Numeric(18, 8),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "payments",
        sa.Column(
            "base_amount",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute("UPDATE payments SET base_amount = amount")

    # --- fx_rate_snapshots: rate cache -----------------------------------
    # Not company-scoped — FX rates are global. One row per
    # (date, source, from_ccy, to_ccy) tuple; unique constraint keeps
    # the cache coherent across repeated fetches. ``fetched_at`` lets
    # us purge stale rows later without losing the origin timestamp.
    op.create_table(
        "fx_rate_snapshots",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("from_ccy", sa.String(3), nullable=False),
        sa.Column("to_ccy", sa.String(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "rate_date",
            "source",
            "from_ccy",
            "to_ccy",
            name="uq_fx_rate_snapshots_key",
        ),
    )
    op.create_index(
        "ix_fx_rate_snapshots_lookup",
        "fx_rate_snapshots",
        ["from_ccy", "to_ccy", "rate_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fx_rate_snapshots_lookup", table_name="fx_rate_snapshots"
    )
    op.drop_table("fx_rate_snapshots")

    for col in ("base_amount", "fx_rate", "currency"):
        op.drop_column("payments", col)

    for table in ("bills", "invoices"):
        for col in (
            "base_amount_paid",
            "base_total",
            "base_tax_total",
            "base_subtotal",
            "fx_rate",
            "currency",
        ):
            op.drop_column(table, col)
