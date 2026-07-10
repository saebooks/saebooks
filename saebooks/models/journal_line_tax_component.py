"""Per-journal-line tax components (M1.5 · T2).

A journal line historically carried a single ``gst_amount`` scalar plus a
``tax_treatment`` JSONB snapshot — enough for one tax on one line, but
structurally unable to represent two or more taxes applying at once:
India CGST+SGST, US state+county+city sales tax, an excise duty that is
then itself subject to VAT, or a reverse-charge output/input pair.

This child table normalises tax into 1:many rows keyed to the journal
line, so co-existing components are first-class, queryable data rather
than a blob. Each row records which canonical ``tax_family`` it belongs
to (T1), its role in a stack, the rate applied, and the base/tax amounts.

Populated by ``services.journal._apply_tax_treatment`` (the single central
snapshot point) — one component per line for now (the AU engine returns a
single treatment); the schema is ready for jurisdiction engines that
return several.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T2).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class JournalLineTaxComponent(Base):
    __tablename__ = "journal_line_tax_components"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    journal_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_lines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised tenancy for RLS + the (company_id) membership check,
    # mirroring how journal_lines carries company_id. Populated from the
    # parent line/entry at insert time.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=_DEFAULT_TENANT_ID,
    )
    # Canonical family (T1): vat_gst | us_sales_use | excise | ... — lets a
    # report/engine reason about the component without the local code name.
    tax_family: Mapped[str] = mapped_column(String(16), nullable=False)
    # Role within a possible stack of components on one line, e.g.
    # 'standard', 'cgst', 'sgst', 'state', 'county', 'city',
    # 'reverse_charge_output', 'reverse_charge_input', 'excise'.
    component_role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="standard"
    )
    # Canonical tax-code string from the tax engine (e.g. 'GST', 'FRE').
    ref_tax_code: Mapped[str | None] = mapped_column(String(32))
    rate_applied: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), nullable=False, default=Decimal("0")
    )
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    # 'output' (sales) | 'input' (purchases) | 'none' (plumbing lines).
    direction: Mapped[str] = mapped_column(
        String(8), nullable=False, default="none"
    )
    # Ordering for stacked components (excise-then-VAT etc.).
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    journal_line = relationship("JournalLine", back_populates="tax_components")
