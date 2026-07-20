"""Recommended chart of accounts per jurisdiction (used at company creation)."""
import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class ChartTemplate(ReferenceBase):
    __tablename__ = "chart_template"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "account_code",
            name="uq_chart_template_jur_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="ASSET | LIABILITY | EQUITY | INCOME | EXPENSE | COST_OF_SALES | OTHER_INCOME | OTHER_EXPENSE",
    )
    default_tax_code: Mapped[str | None] = mapped_column(String(32))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # M1.5 · T10b — statutory-framework scoping (all nullable; AU rows stay
    # NULL because Australia mandates no chart-of-accounts numbering plan).
    # statutory_framework_code is a RefStatutoryAccountFramework.code within
    # the same jurisdiction (matched by (jurisdiction, code), not by id).
    statutory_framework_code: Mapped[str | None] = mapped_column(
        String(32),
        comment="Framework this template row belongs to, e.g. 'skr03'; NULL = jurisdiction default chart.",
    )
    statutory_account_code: Mapped[str | None] = mapped_column(
        String(32),
        comment="Mandated account number under the framework, e.g. SKR03 '4400'.",
    )
    statutory_account_label_local: Mapped[str | None] = mapped_column(
        String(255),
        comment="Local-language statutory label, e.g. 'Erlöse 19 % USt'.",
    )
    statutory_parent_class: Mapped[str | None] = mapped_column(
        String(64),
        comment="Framework class/group the account sits under, e.g. 'Klasse 4'.",
    )
