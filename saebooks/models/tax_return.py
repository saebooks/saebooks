"""A generated return — figures, status, optional lodgement link."""
import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class TaxReturnStatus(enum.StrEnum):
    DRAFT = "draft"
    READY = "ready"
    LODGED = "lodged"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMENDED = "amended"
    # 0199 (Packet 4c) — the manual "file-and-confirm" transition, distinct
    # from LODGED (set only by the automated SBR/X-Road dispatch in
    # api/v1/tax_returns.py's /lodge). A return an accountant filed
    # themselves outside any automated rail (e.g. via EMTA's e-service)
    # is marked FILED via POST /{id}/file, stamping ``filed_at``.
    FILED = "filed"


class TaxReturn(CompanyScoped, Base):
    __tablename__ = "tax_returns"

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
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    jurisdiction: Mapped[str] = mapped_column(String(3), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    return_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="BAS | IAS | GST101 | VAT100 | KMD | KMD-INF | INF-EU | OSS-Q",
    )
    figures: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    generated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[TaxReturnStatus] = mapped_column(
        Enum(
            TaxReturnStatus,
            name="tax_return_status",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
        default=TaxReturnStatus.DRAFT,
    )
    lodgement_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lodgement_records.id", ondelete="SET NULL")
    )
    # 0199 (Packet 4c) — stamped when POST /{id}/file transitions status to
    # FILED. NULL for every return never manually filed through this path.
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- EE X-Road / KMD3 async-filing reference columns (M3, Option A) -------
    # The async submit→poll→confirm lifecycle forces the feedbackReportId UUID
    # and the filing state to persist BETWEEN calls. These are additive, nullable
    # ref columns on the existing return row (migration 0196) — no new tenant
    # table, so no RLS checklist. Populated only for EE returns filed over
    # X-Road; NULL everywhere else.
    ee_filing_request_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="EE X-Road KMD3 feedbackReportId (UUID) — the poll handle.",
    )
    ee_filing_state: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment=(
            "EE filing lifecycle state — EEFilingState value "
            "(submitted|pending|accepted|rejected|confirmed)."
        ),
    )
    ee_filing_receipt: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "EE X-Road feedback receipt — parsed operationAccepted/Rejected "
            "(vatPayable/overpaidVat, errors) / koondvaade JSON."
        ),
    )
