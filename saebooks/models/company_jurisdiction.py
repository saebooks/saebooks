"""Company ↔ jurisdiction membership (M1.5 · 5-SUBJURIS, K5 breadth).

``Company.jurisdiction`` is a single scalar routing key — one company,
one home jurisdiction. The K5 audit flagged the missing breadth half: a
company that operates across multiple sub-national jurisdictions (a
payroll-tax employer in QLD *and* NSW, a landholder in two states, a US
company with sales-tax nexus in several states) has nowhere to record
that membership. This m2m is that record: one row per (company,
jurisdiction) the company operates in.

``jurisdiction_code`` uses the REFERENCE-DB jurisdiction vocabulary
(T3 tree): ISO 3166-1 alpha-3 for countries ('AUS') and ISO 3166-2 for
sub-national nodes ('AU-QLD') — see
``models/reference/jurisdiction.py``. It is free text, non-FK, because
the reference DB is a separate database with no cross-DB foreign key
(same posture as ``Company.jurisdiction`` and
``Company.entity_structure_code``); a caller that wants tree validation
resolves it at the service layer against the reference DB.

Purely additive: nothing reads this table on the posting path;
``Company.jurisdiction`` remains the routing key. AU behaviour is
unchanged by construction.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class CompanyJurisdiction(CompanyScoped, Base):
    """One jurisdiction a company operates in (m2m membership row)."""

    __tablename__ = "company_jurisdictions"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "jurisdiction_code",
            name="uq_company_jurisdictions_natkey",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Reference-DB T3 tree code: 'AUS' (country) or 'AU-QLD' (sub-national).
    # Free text, non-FK — cross-DB (see module docstring).
    jurisdiction_code: Mapped[str] = mapped_column(String(6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
