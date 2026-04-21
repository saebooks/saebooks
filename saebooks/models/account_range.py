"""Account ranges — configurable top-level code prefixes.

Each company defines its own set of account ranges. The prefix can be
any length (1, 2, 10, 200, etc.). Code parsing uses longest-prefix
match to determine which range an account belongs to.

Code structure (when structured numbering is ON):
  {prefix}{child1}{child2}{child3}{child4}{child5}[-{bustard}]

  - prefix:  registered range code (any width)
  - child1-5: one digit each, up to 5 levels of hierarchy
  - bustard:  single letter after hyphen — the "come on you bastard,
              just one more level" overflow when 5 isn't enough
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class AccountRange(CompanyScoped, Base):
    __tablename__ = "account_ranges"
    __table_args__ = (
        UniqueConstraint("company_id", "prefix", name="uq_account_ranges_company_prefix"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    prefix: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="Top-level code prefix (e.g. '1', '10', '200')",
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    account_types: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False,
        comment="Allowed AccountType values for this range",
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
