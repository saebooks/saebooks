import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.tax_code import TaxCode

# Canonical AU GST starter set — users can edit or add more.
AU_SEED: list[dict[str, object]] = [
    {
        "code": "GST",
        "name": "GST 10%",
        "rate": Decimal("10.000"),
        "reporting_type": "taxable",
        "description": "Standard 10% GST on purchases and sales",
    },
    {
        "code": "CAP",
        "name": "GST on capital acquisitions",
        "rate": Decimal("10.000"),
        "reporting_type": "taxable",
        "description": "GST on capital purchases — reported separately (BAS G10)",
    },
    {
        "code": "FRE",
        "name": "GST Free",
        "rate": Decimal("0.000"),
        "reporting_type": "gst_free",
        "description": "GST-free supplies (basic food, health, exports)",
    },
    {
        "code": "INP",
        "name": "Input Taxed",
        "rate": Decimal("0.000"),
        "reporting_type": "input_taxed",
        "description": "Input-taxed supplies (residential rent, financial supplies)",
    },
    {
        "code": "EXP",
        "name": "Export (GST Free)",
        "rate": Decimal("0.000"),
        "reporting_type": "gst_free",
        "description": "Exports — GST-free when conditions met",
    },
    {
        "code": "N-T",
        "name": "Not Reportable",
        "rate": Decimal("0.000"),
        "reporting_type": "out_of_scope",
        "description": "Out of scope for GST (wages, drawings, transfers)",
    },
]


async def ensure_au_seed(session: AsyncSession, company_id: uuid.UUID) -> int:
    """Idempotent: create any missing canonical AU tax codes for this company."""
    existing = await session.execute(
        select(TaxCode.code).where(
            TaxCode.company_id == company_id, TaxCode.archived_at.is_(None)
        )
    )
    have = {code for (code,) in existing.all()}
    inserted = 0
    for row in AU_SEED:
        if row["code"] in have:
            continue
        session.add(TaxCode(company_id=company_id, tax_system="GST", **row))
        inserted += 1
    await session.commit()
    return inserted


async def list_active(session: AsyncSession, company_id: uuid.UUID) -> list[TaxCode]:
    result = await session.execute(
        select(TaxCode)
        .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
        .order_by(TaxCode.code)
    )
    return list(result.scalars().all())


async def get(session: AsyncSession, tax_code_id: uuid.UUID) -> TaxCode | None:
    return await session.get(TaxCode, tax_code_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    name: str,
    rate: Decimal,
    tax_system: str = "GST",
    reporting_type: str = "taxable",
    description: str | None = None,
) -> TaxCode:
    tax_code = TaxCode(
        company_id=company_id,
        code=code.strip(),
        name=name.strip(),
        rate=rate,
        tax_system=tax_system,
        reporting_type=reporting_type,
        description=description,
    )
    session.add(tax_code)
    await session.commit()
    await session.refresh(tax_code)
    return tax_code


async def update(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    code: str | None = None,
    name: str | None = None,
    rate: Decimal | None = None,
    tax_system: str | None = None,
    reporting_type: str | None = None,
    description: str | None = None,
) -> TaxCode:
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        raise ValueError(f"Tax code {tax_code_id} not found")
    if code is not None:
        tax_code.code = code.strip()
    if name is not None:
        tax_code.name = name.strip()
    if rate is not None:
        tax_code.rate = rate
    if tax_system is not None:
        tax_code.tax_system = tax_system
    if reporting_type is not None:
        tax_code.reporting_type = reporting_type
    if description is not None:
        tax_code.description = description or None
    await session.commit()
    await session.refresh(tax_code)
    return tax_code


async def archive(session: AsyncSession, tax_code_id: uuid.UUID) -> None:
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        return
    tax_code.archived_at = datetime.now(UTC)
    await session.commit()
