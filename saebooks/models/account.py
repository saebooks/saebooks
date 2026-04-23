import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class AccountType(enum.StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    OTHER_INCOME = "OTHER_INCOME"
    EXPENSE = "EXPENSE"
    COST_OF_SALES = "COST_OF_SALES"
    OTHER_EXPENSE = "OTHER_EXPENSE"


class Account(CompanyScoped, Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_accounts_company_code"),)

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
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type_enum"), nullable=False
    )
    tax_code_default: Mapped[str | None] = mapped_column(String)
    is_header: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconcile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    system_managed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="System-managed accounts (GST, etc.) — auto-posted by the engine",
    )
    # ABA / Direct Entry — populated only on bank accounts. Remitter
    # side of the APCA agreement: BSB + account + account title, plus
    # the sponsor bank's 6-digit User ID and 3-letter abbreviation.
    bsb: Mapped[str | None] = mapped_column(
        String(7), comment="BSB formatted 'xxx-xxx' (ABA remitter)"
    )
    bank_account_number: Mapped[str | None] = mapped_column(String(9))
    bank_account_title: Mapped[str | None] = mapped_column(
        String(32), comment="Account title on the bank statement (ABA field)"
    )
    apca_user_id: Mapped[str | None] = mapped_column(
        String(6), comment="6-digit Direct Entry User ID from sponsor bank"
    )
    bank_abbreviation: Mapped[str | None] = mapped_column(
        String(3), comment="3-letter ABA bank code — CBA, ANZ, NAB, WBC, …"
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Optimistic-locking version — bumped on every write through the API.
    # Jinja routes that call the service layer without expected_version skip
    # the guard (last-writer-wins, same behaviour as before Phase 1).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
