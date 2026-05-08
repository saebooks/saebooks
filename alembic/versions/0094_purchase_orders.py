"""Purchase orders + PO lines.

Mirror of ``bills`` (0022) plus modern conventions inherited
from later batches in one shot — no follow-up "version + tenant +
FX backfill" migration needed because the model is born modern:

* ``version INT`` for optimistic locking (If-Match / version param)
* ``tenant_id UUID`` + RLS policy ``tenant_isolation`` (FORCE)
* FX columns: ``currency``, ``fx_rate``, ``base_subtotal``,
  ``base_tax_total``, ``base_total``
* ``external_id`` / ``external_source`` / ``external_etag`` /
  ``external_payload`` for cross-system reconciliation (matches the
  shape of 0092)
* ``archived_at`` for soft-delete

PO-specific columns vs Bill
---------------------------
* ``status`` enum widens to DRAFT / OPEN / PARTIAL / RECEIVED /
  CLOSED / CANCELLED. Distinct from bill statuses because a PO is a
  commitment, not a posting; "POSTED/VOIDED" doesn't fit the
  procurement lifecycle.
* ``expected_date`` instead of ``due_date`` — when goods arrive, not
  when payment lands.
* ``delivery_address`` — free text; we don't normalise to a separate
  table yet.
* ``sent_at`` / ``closed_at`` / ``cancelled_at`` — three lifecycle
  timestamps so the UI can show "sent 4 days ago", "closed 2 weeks
  ago".
* No ``journal_entry_id`` / ``void_journal_entry_id`` — the PO has
  no GL impact. Bills are still the financial event.
* No ``amount_paid`` / ``base_amount_paid`` — same reason.

PO line additions
-----------------
* ``received_qty NUMERIC(18, 4) NOT NULL DEFAULT 0`` — convert-to-bill
  advances this. Together with ``quantity`` it gives "outstanding =
  quantity - received_qty" without re-querying child bills. Once
  every line is fully received the PO auto-flips to RECEIVED and
  the user can close it with one click.

Class-A RLS
-----------
``purchase_orders`` carries ``tenant_id`` directly so the
``tenant_isolation`` policy from 0055 applies verbatim.
``purchase_order_lines`` is scoped via parent (mirrors how
``bill_lines`` and ``invoice_lines`` are handled — no policy on the
child table; access is gated by joining through the parent in every
service-layer query).

Revision ID: 0094_purchase_orders
Revises: 0093_cashbook_mode
Create Date: 2026-05-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0094_purchase_orders"
down_revision: str | None = "0093_cashbook_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PO_STATUSES = (
    "DRAFT",
    "OPEN",
    "PARTIAL",
    "RECEIVED",
    "CLOSED",
    "CANCELLED",
)
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    po_status = postgresql.ENUM(*PO_STATUSES, name="purchase_order_status_enum")
    po_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "purchase_orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.String(32)),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("expected_date", sa.Date()),
        sa.Column(
            "status",
            postgresql.ENUM(
                *PO_STATUSES,
                name="purchase_order_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AUD"),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=False, server_default="1"),
        sa.Column("base_subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("base_tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("base_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("delivery_address", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("external_id", sa.String(255)),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_etag", sa.String(255)),
        sa.Column("external_payload", postgresql.JSONB()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "company_id", "number", name="uq_purchase_orders_company_number"
        ),
    )
    op.create_index(
        "ix_purchase_orders_company_status",
        "purchase_orders",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_purchase_orders_company_contact",
        "purchase_orders",
        ["company_id", "contact_id"],
    )
    op.create_index(
        "ix_purchase_orders_company_expected",
        "purchase_orders",
        ["company_id", "expected_date"],
    )
    op.create_index(
        "ix_purchase_orders_external",
        "purchase_orders",
        ["company_id", "external_source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    # Class-A RLS: enable + force + tenant_isolation policy.
    op.execute("ALTER TABLE purchase_orders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE purchase_orders FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON purchase_orders "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    )

    op.create_table(
        "purchase_order_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "purchase_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("discount_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("line_subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("line_tax", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("received_qty", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_purchase_order_lines_po",
        "purchase_order_lines",
        ["purchase_order_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_purchase_order_lines_po", table_name="purchase_order_lines"
    )
    op.drop_table("purchase_order_lines")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON purchase_orders")
    op.execute("ALTER TABLE purchase_orders NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE purchase_orders DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_purchase_orders_external", table_name="purchase_orders")
    op.drop_index(
        "ix_purchase_orders_company_expected", table_name="purchase_orders"
    )
    op.drop_index(
        "ix_purchase_orders_company_contact", table_name="purchase_orders"
    )
    op.drop_index(
        "ix_purchase_orders_company_status", table_name="purchase_orders"
    )
    op.drop_table("purchase_orders")

    postgresql.ENUM(
        *PO_STATUSES, name="purchase_order_status_enum"
    ).drop(op.get_bind(), checkfirst=True)
