"""SuperFund model — APRA-regulated funds + SMSFs.

One row per (company, fund). APRA funds (Sunsuper, AustralianSuper,
ART, Hostplus, ...) are identified by USI. SMSFs are identified by
ABN + ESA + bank details — discriminator is ``is_smsf``.

Exactly one fund may be flagged ``is_default = True`` per company,
enforced via partial unique index. Service layer falls back to the
default fund when creating employees without an explicit fund.

SMSF bank fields are Fernet ciphertext via
``saebooks.services.crypto``. APRA USIs are public reference data —
plaintext.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class SuperFund(CompanyScoped, Base):
    __tablename__ = "super_funds"
    __table_args__ = (
        CheckConstraint(
            "is_smsf = false OR (employer_abn IS NOT NULL AND esa IS NOT NULL)",
            name="ck_super_funds_smsf_required_fields",
        ),
        CheckConstraint(
            "is_smsf = true OR usi IS NOT NULL",
            name="ck_super_funds_apra_requires_usi",
        ),
        CheckConstraint(
            "usi IS NULL OR length(usi) = 11",
            name="ck_super_funds_usi_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    usi: Mapped[str | None] = mapped_column(String(11))
    spin: Mapped[str | None] = mapped_column(String(20))
    is_smsf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    employer_abn: Mapped[str | None] = mapped_column(String(14))
    esa: Mapped[str | None] = mapped_column(String(16))
    smsf_bsb_encrypted: Mapped[str | None] = mapped_column(Text)
    smsf_account_number_encrypted: Mapped[str | None] = mapped_column(Text)
    smsf_account_name_encrypted: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
