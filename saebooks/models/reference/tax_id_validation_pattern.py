"""Regex + checksum metadata for ABN, NZBN, VAT IDs, EE reg.code, etc."""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxIdValidationPattern(ReferenceBase):
    __tablename__ = "tax_id_validation_patterns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    pattern_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="abn | acn | tfn | nzbn | vat | reg_code | tax_residency",
    )
    regex: Mapped[str] = mapped_column(String, nullable=False)
    checksum_algorithm: Mapped[str | None] = mapped_column(
        String(64),
        comment="abn-mod89 | nz-bn-mod10 | vat-modulo-97 | none",
    )
