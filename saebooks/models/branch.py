"""Branch — a sub-divisional tag on transactions, scoped per Company.

Branches are internal cost/location/division tags. NOT a legal entity
— that's what Company is for. Example: Example Trust runs business
activity under the trading name "SAE Engineering" as a single branch;
if the trust ever splits into multiple operating divisions, each gets
its own branch row and transactions get tagged accordingly.

Schema-side:
  - branches(id, company_id, tenant_id, code, name, is_default,
             archived_at, version, created_at)
  - unique (company_id, code)
  - partial unique on (company_id) WHERE is_default = true (one default
    per company)
  - FORCE RLS + tenant_isolation policy
  - tenant-coherence trigger asserts branches.tenant_id == companies.tenant_id

Most transactional tables (journal_entries, invoices, bills,
bank_statement_lines, payments, credit_notes, expenses) gained a
nullable ``branch_id`` FK in migration 0134. A per-table coherence
trigger asserts branch.company_id == row.company_id when set.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_branches_company_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default="false",
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    version: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=1, server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
