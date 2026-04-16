import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class AccountType(enum.StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    OTHER_INCOME = "OTHER_INCOME"
    EXPENSE = "EXPENSE"
    COST_OF_SALES = "COST_OF_SALES"
    OTHER_EXPENSE = "OTHER_EXPENSE"


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_accounts_company_code"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
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
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
