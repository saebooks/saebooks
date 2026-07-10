"""Box-by-box layout of every tax return form per jurisdiction."""
import uuid

from sqlalchemy import ARRAY, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxReturnBoxDefinition(ReferenceBase):
    __tablename__ = "tax_return_box_definitions"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "return_type", "box_code",
            name="uq_box_def_jur_form_box",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    return_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="BAS / IAS / GST101 / VAT100 / KMD / KMD-INF / INF-EU / OSS-Q",
    )
    box_code: Mapped[str] = mapped_column(String(32), nullable=False)
    box_label: Mapped[str] = mapped_column(String(256), nullable=False)
    aggregation: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "sum_tax_amount_for_codes | sum_taxable_for_codes | formula | manual "
            "-- when 'formula', the expression lives in the 'formula' column "
            "(not inlined here: box 4's rate-formula overflows String(64) — "
            "see M1.5 KMD-formula-support Packet 1)"
        ),
    )
    feeder_tax_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    formula: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Box-arithmetic expression for aggregation='formula' boxes: box "
            "references (<return_type>:<box_code> or bare <box_code>), "
            "decimal literals, + - * operators, and max(0, <expr>). Parsed/"
            "evaluated by tax_return_generator (safe AST, no eval()). NULL "
            "for every non-formula box."
        ),
    )
