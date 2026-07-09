"""ORM model for per-supplier statement-extraction templates (P4).

A ``SupplierStatementTemplate`` carries a supplier-specific extraction hint
(and optional page scope) that is injected into the LLM prompt when a statement
from that supplier is (re-)ingested. It exists to tame layout variability the
generic prompt can't handle on its own — e.g. "amounts are in the rightmost
column", or "use the page-1 summary only" for multi-page fuel-card statements.

Match keys (any may be null; matched in priority order by the ingest layer):
contact_id (preferred, the resolved supplier) → supplier_abn → supplier_name.

Schema materialised by migration ``0151_supplier_statement_templates``.
RLS Class A (direct tenant_id) + CompanyScoped, same as supplier_statements.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class SupplierStatementTemplate(CompanyScoped, Base):
    """A supplier-specific extraction hint for statement parsing."""

    __tablename__ = "supplier_statement_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=uuid.uuid4, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Match keys (priority: contact_id → supplier_abn → supplier_name).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    supplier_abn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The extraction guidance injected into the LLM system prompt.
    prompt_hint: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional structural hint (e.g. "page_1_only") — informational + may guide vision.
    page_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
