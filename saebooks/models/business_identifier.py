import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class BusinessIdentifier(CompanyScoped, Base):
    __tablename__ = "business_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "scheme", name="uq_business_identifiers_company_scheme"
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
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    # Free-text scheme key — the registry of accepted values (au_abn,
    # au_acn, nz_nzbn, uk_crn, ee_regcode, global_lei, ...) lives in
    # the service layer rather than as a DB enum so adding a new
    # jurisdiction never requires an enum-altering migration.
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ---- M1.5 · T9: tax-identifier canonical gaps (additive) ----
    # 3-char jurisdiction code matching saebooks.models.reference.jurisdiction
    # (e.g. 'AUS', 'GBR', 'USA'). No cross-DB FK — the jurisdiction registry
    # lives in the reference DB, same posture as companies.jurisdiction.
    # NULL = not yet classified. The service layer derives a default from
    # the scheme prefix on upsert (see services.business_identifiers
    # ``_derive_jurisdiction``) rather than a single fixed column default,
    # since the right value differs per scheme (au_abn -> AUS, uk_crn -> GBR).
    jurisdiction: Mapped[str | None] = mapped_column(String(3))
    # Result of the scheme's registered check-digit/format validator, if
    # any (services.business_identifiers.KNOWN_SCHEMES / _VALIDATORS).
    # NULL = no validator registered for this scheme, or not yet checked.
    check_digit_valid: Mapped[bool | None] = mapped_column(Boolean)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    issuing_authority: Mapped[str | None] = mapped_column(
        String(128), comment="e.g. 'Australian Business Register', 'Companies House'."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
