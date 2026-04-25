import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.tax_code import TaxCode
from saebooks.services import audit as audit_svc
from saebooks.services import change_log as change_log_svc


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value.

    The API layer catches this and returns 409 with current server state.
    """

    def __init__(self, current: TaxCode) -> None:
        super().__init__(
            f"TaxCode {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


# Columns serialised into change_log.payload
_TAX_CODE_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "code",
    "name",
    "rate",
    "tax_system",
    "reporting_type",
    "description",
    "version",
    "created_at",
    "archived_at",
)


def _serialise(tax_code: TaxCode) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _TAX_CODE_COLUMNS:
        val = getattr(tax_code, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data

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


async def get(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> TaxCode | None:
    """Fetch a tax code by id.

    P0 cross-tenant leak fix: when ``tenant_id`` is supplied, the
    lookup is filtered by tenant — a foreign-tenant id returns
    ``None`` even if the row exists. The parameter is keyword-only
    and optional so existing internal callers keep working unchanged;
    the API layer always supplies it.
    """
    if tenant_id is None:
        return await session.get(TaxCode, tax_code_id)
    result = await session.execute(
        select(TaxCode).where(
            TaxCode.id == tax_code_id,
            TaxCode.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


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
    performed_by: str | None = None,
) -> TaxCode:
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        raise ValueError(f"Tax code {tax_code_id} not found")
    before = audit_svc.capture(tax_code)
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
    await audit_svc.snapshot_row(
        session, tax_code,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    await session.refresh(tax_code)
    return tax_code


async def archive(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        return
    before = audit_svc.capture(tax_code)
    tax_code.archived_at = datetime.now(UTC)
    await audit_svc.snapshot_row(
        session, tax_code,
        action="archive",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# API-oriented helpers (version-aware, change_log wiring)
# The Jinja-facing functions above remain untouched.
# ---------------------------------------------------------------------------


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_for_api(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    name: str,
    rate: Decimal,
    tax_system: str = "GST",
    reporting_type: str = "taxable",
    description: str | None = None,
    actor: str = "api",
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> TaxCode:
    """Create a new tax code and append a change_log row."""
    tax_code = TaxCode(
        company_id=company_id,
        tenant_id=tenant_id,
        code=code.strip(),
        name=name.strip(),
        rate=rate,
        tax_system=tax_system,
        reporting_type=reporting_type,
        description=description,
        version=1,
    )
    session.add(tax_code)
    await session.flush()
    await session.refresh(tax_code)
    await change_log_svc.append(
        session,
        entity="tax_code",
        entity_id=tax_code.id,
        op="create",
        actor=actor,
        payload=_serialise(tax_code),
        version=tax_code.version,
    )
    await session.commit()
    return tax_code


async def update_with_version(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    code: str | None = None,
    name: str | None = None,
    rate: Decimal | None = None,
    tax_system: str | None = None,
    reporting_type: str | None = None,
    description: str | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
) -> TaxCode:
    """Update a tax code with optimistic locking + change_log."""
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        raise ValueError(f"Tax code {tax_code_id} not found")

    if expected_version is not None and tax_code.version != expected_version:
        raise VersionConflict(tax_code)

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

    tax_code.version = tax_code.version + 1
    await session.flush()
    await session.refresh(tax_code)
    await change_log_svc.append(
        session,
        entity="tax_code",
        entity_id=tax_code.id,
        op="update",
        actor=actor or "api",
        payload=_serialise(tax_code),
        version=tax_code.version,
    )
    await session.commit()
    return tax_code


async def archive_with_version(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    expected_version: int | None = None,
    actor: str | None = None,
) -> TaxCode | None:
    """Soft-archive a tax code with optimistic locking + change_log."""
    tax_code = await session.get(TaxCode, tax_code_id)
    if tax_code is None:
        return None
    if expected_version is not None and tax_code.version != expected_version:
        raise VersionConflict(tax_code)
    tax_code.archived_at = datetime.now(UTC)
    tax_code.version = tax_code.version + 1
    await session.flush()
    await session.refresh(tax_code)
    await change_log_svc.append(
        session,
        entity="tax_code",
        entity_id=tax_code.id,
        op="archive",
        actor=actor or "api",
        payload=_serialise(tax_code),
        version=tax_code.version,
    )
    await session.commit()
    return tax_code
