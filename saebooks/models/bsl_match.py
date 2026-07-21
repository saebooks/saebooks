"""Bank-statement-line match (junction row).

Models one allocation of a bank statement line against one target —
either a payment or a journal entry. Multiple rows per BSL is the
whole point: a single $5,000 deposit can pay 30 invoices, and each
of those payments gets its own ``bsl_matches`` row.

Schema lives in migration ``0077_bsl_matches``. The 1:1 columns on
``bank_statement_lines`` (``matched_entry_id``, ``matched_to_*``)
are kept populated for back-compat with the existing API/UI; this
junction is the new source of truth for status recomputation.

Sign rule: ``amount`` is signed and must agree with the BSL's amount
sign (deposit BSL → positive allocation; withdrawal → negative).
The service layer enforces this — there is no DB check because PG
can't reach the parent row from a check constraint without a
trigger, and we'd rather have a clear Python error than a generic
constraint violation.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

TARGET_PAYMENT = "PAYMENT"
TARGET_JOURNAL_ENTRY = "JOURNAL_ENTRY"


class BslMatch(CompanyScoped, Base):
    __tablename__ = "bsl_matches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bsl_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bank_statement_lines.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=_DEFAULT_TENANT_ID,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    matched_by: Mapped[str | None] = mapped_column(String)
    # Provenance (M3 R8b, migration 0220) — how this allocation came to
    # exist. MANUAL: /reconciliation/match or split_match. AUTO:
    # /reconciliation/auto_match (exactly-one-HIGH-confidence linking).
    # RULE: a bank_rules match. COMPOUND: /reconciliation/create_and_match
    # (create + post + match in one call). Defaults to MANUAL so every
    # pre-0220 row backfills correctly.
    matched_via: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MANUAL"
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bank_rules.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
