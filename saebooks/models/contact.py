import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class ContactType(enum.StrEnum):
    CUSTOMER = "CUSTOMER"
    SUPPLIER = "SUPPLIER"
    BOTH = "BOTH"
    BENEFICIARY = "BENEFICIARY"


class Contact(CompanyScoped, Base):
    __tablename__ = "contacts"

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
    name: Mapped[str] = mapped_column(String, nullable=False)
    contact_type: Mapped[ContactType] = mapped_column(
        Enum(ContactType, name="contact_type_enum"), nullable=False
    )
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String(32))
    abn: Mapped[str | None] = mapped_column(
        String(14), comment="Australian Business Number — 11 digits stored as 'xx xxx xxx xxx'"
    )
    address_line1: Mapped[str | None] = mapped_column(String)
    address_line2: Mapped[str | None] = mapped_column(String)
    city: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(
        String(8), comment="AU state code e.g. NSW, VIC, QLD"
    )
    postcode: Mapped[str | None] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(64), default="Australia")
    notes: Mapped[str | None] = mapped_column(Text)
    default_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    default_tax_code: Mapped[str | None] = mapped_column(String(16))
    # ABA / Direct Entry — payee side. Used when the contact is a
    # supplier that we pay via Direct Entry bank file. All three must
    # be set for the contact to appear as an ABA-eligible payee in
    # the pay-run UI.
    bank_bsb: Mapped[str | None] = mapped_column(
        String(7), comment="BSB formatted 'xxx-xxx' (ABA payee)"
    )
    bank_account_number: Mapped[str | None] = mapped_column(String(9))
    bank_account_title: Mapped[str | None] = mapped_column(
        String(32), comment="Name on the payee's bank account (ABA field)"
    )
    # Beneficiary-specific fields. Only populated when contact_type = BENEFICIARY.
    tfn: Mapped[str | None] = mapped_column(
        String(11), comment="Tax File Number — 8 or 9 digits without spaces"
    )
    share_percentage: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4), comment="Default entitlement share 0.0000 – 100.0000"
    )
    default_income_classification: Mapped[str | None] = mapped_column(
        String(64), comment="e.g. Individual, Company, Trust, SMSF"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    currency_code: Mapped[str | None] = mapped_column(
        String(3), comment="ISO 4217 billing currency, e.g. JPY, USD. NULL implies AUD."
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # TPAR: flag this contact as a sub-contractor for TPAR reporting (CIVL-5).
    is_tpar_supplier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # One-off / walk-in flag — keeps transient parties out of the main list
    # (filterable on the contacts page; toggled via bulk-tag-one-off).
    is_one_off: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Monotonic version counter for optimistic locking via the API's
    # ``If-Match: <version>`` header. Bumped on every write that goes
    # through ``saebooks.api.v1``; legacy Jinja writes also route
    # through the same service layer so the counter stays authoritative.
    version: Mapped[int] = mapped_column(default=1, nullable=False)

