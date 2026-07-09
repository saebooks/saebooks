"""Per-jurisdiction tax codes (sale/purchase rates feeding return boxes)."""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    true as sa_true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxDirection(enum.StrEnum):
    SALE = "sale"
    PURCHASE = "purchase"
    BOTH = "both"


class TaxFamily(enum.StrEnum):
    """Jurisdiction-neutral tax families (M1.5 · T1).

    The canonical concept behind every indirect tax. Richard's guiding
    example: GST, VAT, TVA, IVA, Mehrwertsteuer are all the *same* family
    (value-added, multi-stage, input-creditable) — ``VAT_GST`` — while US
    sales-&-use tax is a *different* family (single-stage, no input credit).
    Regional names resolve to one of these; ``input_credit_recoverable``
    captures the credit consequence that distinguishes the families.
    See docs/multi-jurisdiction.md (M1.5) (theme T1).
    """

    VAT_GST = "vat_gst"            # GST / VAT / TVA / IVA / MwSt — input-creditable
    US_SALES_USE = "us_sales_use"  # US sales & use tax — single-stage, no input credit
    EXCISE = "excise"              # specific / per-unit duties (fuel, alcohol, tobacco)
    CUSTOMS_DUTY = "customs_duty"  # import/customs duties
    WITHHOLDING = "withholding"    # withholding taxes on payments
    OTHER = "other"


TAX_FAMILIES = tuple(f.value for f in TaxFamily)


class RefTaxCode(ReferenceBase):
    """Reference tax code. NOT the same as ``saebooks.models.tax_code.TaxCode``,
    which is per-company. Companies pick a reference code and may override
    the name/description; the rate is sourced from here.
    """

    __tablename__ = "tax_codes"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_ref_tax_codes_jur_code_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        default=Decimal("0"),
        comment="Rate as a percentage (10.0000 = 10%, not 0.1)",
    )
    direction: Mapped[TaxDirection] = mapped_column(
        Enum(
            TaxDirection,
            name="ref_tax_direction",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
    )
    is_inclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reverse_charge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # M1.5 · T1 — canonical tax family + credit consequence. Additive/defaulted
    # so existing rows become VAT_GST (all seeded codes to date are AU GST).
    tax_family: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="vat_gst",
        server_default="vat_gst",
        comment="One of TAX_FAMILIES — the jurisdiction-neutral family (GST/VAT = vat_gst).",
    )
    input_credit_recoverable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_true(),
        comment="True for VAT/GST input credits; False for single-stage sales/use tax.",
    )
    gl_account_hint: Mapped[str | None] = mapped_column(
        String(64),
        comment="Free text hint, e.g. 'GST Payable'. Not an FK — chart of accounts lives in the company DB.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    report_box_keys: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        comment="Box keys this code feeds into, e.g. ['BAS:G1', 'BAS:1A']",
    )
