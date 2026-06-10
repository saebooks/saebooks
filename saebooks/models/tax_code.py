import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class TaxCode(CompanyScoped, Base):
    __tablename__ = "tax_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    tax_system: Mapped[str] = mapped_column(String(16), nullable=False, default="GST")
    # Jurisdiction tag (0165). Existing AU rows default to 'AU'. Lets the
    # engine hold per-jurisdiction code sets (AU GST, NZ GST, UK VAT...)
    # without code collisions — the active-row unique index is
    # (company_id, jurisdiction, code). The app surfaces only the home
    # jurisdiction (AU) so international reference codes stay hidden.
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, default="AU")
    reporting_type: Mapped[str] = mapped_column(String(32), nullable=False, default="taxable")
    description: Mapped[str | None] = mapped_column(String)
    # Optimistic-locking version — bumped on every write through the API.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
