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
    "jurisdiction",
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
        "reporting_type": "capital",
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
        "reporting_type": "export",
        "description": "Exports — GST-free when conditions met",
    },
    {
        "code": "N-T",
        "name": "Not Reportable",
        "rate": Decimal("0.000"),
        "reporting_type": "out_of_scope",
        "description": "Out of scope for GST (wages, drawings, transfers)",
    },
    {
        "code": "MGN",
        "name": "Margin Scheme (Div 75 s66-50)",
        "rate": Decimal("0.000"),
        "reporting_type": "margin_scheme",
        "description": (
            "Used-vehicle / real-property margin scheme — GST = 1/11 × "
            "(sale price − acquisition cost). Enter acquisition cost in the "
            "Acq. Cost field on each line. Div 75 GSTA 1999 s66-50."
        ),
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
        session.add(
            TaxCode(
                company_id=company_id,
                tax_system="GST",
                jurisdiction="AU",
                **row,
            )
        )
        inserted += 1
    await session.commit()
    return inserted


# Canonical EE käibemaks (VAT) starter set — the company-side counterpart of
# AU_SEED, sized to the same "small starter, user can add more" bar. Codes
# match the account-level ``tax_code_default`` values the EE chart template
# assigns (STD / INPUT_STD / ZERO_EXPORT / INPUT_EXEMPT / NTR — see
# jurisdictions/ee/chart.py) so a freshly-charted EE company's account defaults
# resolve, plus the reduced rates and the EU-acquisition reverse-charge code.
# ``reporting_type`` follows the KMD company-side convention documented in
# seeds/jurisdictions/EE/tax_codes.yaml; ``rate`` drives posting (the EE tax
# engine computes tax = base * rate/100, direction from account type). The
# reverse-charge code carries ``rc_eu_acq_services`` — the tag EETaxEngine's
# RC_DUAL_REPORTING_TYPES fans out into a self-assessed output + deductible
# input component pair. Standard rate 24% (KMS §15, from 2025-07-01).
EE_SEED: list[dict[str, object]] = [
    {"code": "STD", "name": "Käibemaks 24%", "rate": Decimal("24.000"),
     "reporting_type": "standard",
     "description": "Standard-rate käibemaks (VAT) on domestic sales (24%)."},
    {"code": "RED13", "name": "Käibemaks 13% (majutus)", "rate": Decimal("13.000"),
     "reporting_type": "reduced_13",
     "description": "Reduced-rate VAT 13% — accommodation services."},
    {"code": "RED9", "name": "Käibemaks 9% (raamatud, ravimid)", "rate": Decimal("9.000"),
     "reporting_type": "reduced_9",
     "description": "Reduced-rate VAT 9% — books, press, medicines."},
    {"code": "ZERO_EXPORT", "name": "0% — eksport / ühendusesisene käive",
     "rate": Decimal("0.000"), "reporting_type": "zero_export",
     "description": "Zero-rated exports and intra-Community supplies (KMS §15(3)-(4))."},
    {"code": "EXEMPT", "name": "Maksuvaba käive", "rate": Decimal("0.000"),
     "reporting_type": "exempt",
     "description": "VAT-exempt supplies — financial, insurance, health (KMS §16)."},
    {"code": "INPUT_STD", "name": "Sisendkäibemaks 24%", "rate": Decimal("24.000"),
     "reporting_type": "input_std",
     "description": "Deductible input VAT 24% on domestic purchases."},
    {"code": "INPUT_EXEMPT", "name": "Maksuvaba ost", "rate": Decimal("0.000"),
     "reporting_type": "input_exempt",
     "description": "Exempt purchases (e.g. bank fees) — no input VAT."},
    {"code": "RC_EU_ACQ", "name": "Pöördmaksustamine — EU teenuste soetamine",
     "rate": Decimal("24.000"), "reporting_type": "rc_eu_acq_services",
     "description": (
         "Reverse-charge EU acquisition of services — recipient self-assesses "
         "VAT (output KMD box 1 + deductible input KMD box 5)."
     )},
    {"code": "NTR", "name": "Ei kuulu deklareerimisele", "rate": Decimal("0.000"),
     "reporting_type": "no_tax",
     "description": "Outside VAT scope (wages, transfers, drawings)."},
]


async def ensure_ee_seed(session: AsyncSession, company_id: uuid.UUID) -> int:
    """Idempotent: create any missing canonical EE käibemaks tax codes for
    this company. Sibling of ``ensure_au_seed``; rows are jurisdiction-tagged
    ``EE`` / ``tax_system=VAT``. Like ``ensure_au_seed`` these carry the model
    DEFAULT tenant_id — the caller (the EE demo seeder) re-stamps tenant_id
    onto them, exactly as the AU path does for RLS isolation."""
    existing = await session.execute(
        select(TaxCode.code).where(
            TaxCode.company_id == company_id, TaxCode.archived_at.is_(None)
        )
    )
    have = {code for (code,) in existing.all()}
    inserted = 0
    for row in EE_SEED:
        if row["code"] in have:
            continue
        session.add(
            TaxCode(
                company_id=company_id,
                tax_system="VAT",
                jurisdiction="EE",
                **row,
            )
        )
        inserted += 1
    await session.commit()
    return inserted


# ---------------------------------------------------------------------------
# International reference tax-code set (0165).
#
# A CURATED standard set — NOT every country. Each entry is jurisdiction-
# tagged so it never collides with the AU codes and never surfaces in the
# AU app (the list endpoints default to the home jurisdiction). The engine
# can resolve these once the per-jurisdiction tax engines (NZ/UK/EE) and a
# company.jurisdiction selector land; until then they are reference data,
# baked in so they are "off the list".
#
# tax_system values: GST (AU/NZ), VAT (UK/EU), SALES_TAX (US), GENERIC.
# reporting_type reuses the AU vocabulary where it maps cleanly
# (taxable / gst_free / input_taxed / export / out_of_scope) and adds
# "reverse_charge" + "reduced_rate" for VAT shapes.
# ---------------------------------------------------------------------------

INTERNATIONAL_SEED: list[dict[str, object]] = [
    # --- New Zealand GST ---
    {"jurisdiction": "NZ", "tax_system": "GST", "code": "NZ_GST", "name": "NZ GST 15%",
     "rate": Decimal("15.000"), "reporting_type": "taxable",
     "description": "New Zealand standard-rate GST (15%)."},
    {"jurisdiction": "NZ", "tax_system": "GST", "code": "NZ_ZERO", "name": "NZ GST Zero-Rated",
     "rate": Decimal("0.000"), "reporting_type": "export",
     "description": "New Zealand zero-rated supplies (exports, going concern)."},
    {"jurisdiction": "NZ", "tax_system": "GST", "code": "NZ_EXEMPT", "name": "NZ GST Exempt",
     "rate": Decimal("0.000"), "reporting_type": "input_taxed",
     "description": "New Zealand exempt supplies (financial services, residential rent)."},
    # --- United Kingdom VAT ---
    {"jurisdiction": "UK", "tax_system": "VAT", "code": "UK_STD", "name": "UK VAT Standard 20%",
     "rate": Decimal("20.000"), "reporting_type": "taxable",
     "description": "United Kingdom standard-rate VAT (20%)."},
    {"jurisdiction": "UK", "tax_system": "VAT", "code": "UK_RED", "name": "UK VAT Reduced 5%",
     "rate": Decimal("5.000"), "reporting_type": "reduced_rate",
     "description": "United Kingdom reduced-rate VAT (5%) — domestic fuel, child seats."},
    {"jurisdiction": "UK", "tax_system": "VAT", "code": "UK_ZERO", "name": "UK VAT Zero-Rated",
     "rate": Decimal("0.000"), "reporting_type": "export",
     "description": "United Kingdom zero-rated supplies (most food, books, exports)."},
    {"jurisdiction": "UK", "tax_system": "VAT", "code": "UK_EXEMPT", "name": "UK VAT Exempt",
     "rate": Decimal("0.000"), "reporting_type": "input_taxed",
     "description": "United Kingdom exempt supplies (insurance, finance, education)."},
    {"jurisdiction": "UK", "tax_system": "VAT", "code": "UK_RC", "name": "UK VAT Reverse Charge",
     "rate": Decimal("20.000"), "reporting_type": "reverse_charge",
     "description": "United Kingdom domestic/EU reverse-charge VAT — recipient accounts for VAT."},
    # --- European Union (generic — member states vary) ---
    {"jurisdiction": "EU", "tax_system": "VAT", "code": "EU_STD", "name": "EU VAT Standard",
     "rate": Decimal("21.000"), "reporting_type": "taxable",
     "description": "EU standard-rate VAT (generic 21% placeholder — set per member state)."},
    {"jurisdiction": "EU", "tax_system": "VAT", "code": "EU_RED", "name": "EU VAT Reduced",
     "rate": Decimal("10.000"), "reporting_type": "reduced_rate",
     "description": "EU reduced-rate VAT (generic 10% placeholder — set per member state)."},
    {"jurisdiction": "EU", "tax_system": "VAT", "code": "EU_ZERO", "name": "EU VAT Zero-Rated",
     "rate": Decimal("0.000"), "reporting_type": "export",
     "description": "EU zero-rated / intra-community supplies."},
    {"jurisdiction": "EU", "tax_system": "VAT", "code": "EU_RC", "name": "EU VAT Reverse Charge",
     "rate": Decimal("0.000"), "reporting_type": "reverse_charge",
     "description": "EU reverse-charge VAT (intra-community B2B) — recipient accounts for VAT."},
    # --- United States sales tax (rate varies by state/locality) ---
    {"jurisdiction": "US", "tax_system": "SALES_TAX", "code": "US_TAX", "name": "US Sales Tax (varies)",
     "rate": Decimal("0.000"), "reporting_type": "taxable",
     "description": "United States sales tax — rate varies by state/locality; set per nexus."},
    {"jurisdiction": "US", "tax_system": "SALES_TAX", "code": "US_NOTAX", "name": "US No Tax",
     "rate": Decimal("0.000"), "reporting_type": "out_of_scope",
     "description": "United States — no sales tax (exempt sale or no nexus)."},
    # --- Generic cross-jurisdiction reference codes ---
    {"jurisdiction": "GEN", "tax_system": "GENERIC", "code": "GEN_ZERO", "name": "Zero-Rated",
     "rate": Decimal("0.000"), "reporting_type": "export",
     "description": "Generic zero-rated supply."},
    {"jurisdiction": "GEN", "tax_system": "GENERIC", "code": "GEN_EXEMPT", "name": "Exempt",
     "rate": Decimal("0.000"), "reporting_type": "input_taxed",
     "description": "Generic exempt / input-taxed supply."},
    {"jurisdiction": "GEN", "tax_system": "GENERIC", "code": "GEN_OOS", "name": "Out of Scope",
     "rate": Decimal("0.000"), "reporting_type": "out_of_scope",
     "description": "Generic out-of-scope (not reportable for indirect tax)."},
    {"jurisdiction": "GEN", "tax_system": "GENERIC", "code": "GEN_RC", "name": "Reverse Charge",
     "rate": Decimal("0.000"), "reporting_type": "reverse_charge",
     "description": "Generic reverse-charge — recipient accounts for the tax."},
]

# Imported-services reverse-charge + GST-on-imports for the AU/BAS set. These
# are AU-jurisdiction codes that complement AU_SEED (kept separate so the
# original AU starter set the app shows stays the lean 7-code list, while the
# full BAS completeness codes are seeded for engine/BAS use).
AU_EXTENDED_SEED: list[dict[str, object]] = [
    {"jurisdiction": "AU", "tax_system": "GST", "code": "RCP", "name": "Reverse Charge — Imported Services",
     "rate": Decimal("10.000"), "reporting_type": "reverse_charge",
     "description": "GST reverse charge on imported services (Div 84) — recipient self-assesses GST."},
    {"jurisdiction": "AU", "tax_system": "GST", "code": "IMP", "name": "GST on Imports",
     "rate": Decimal("10.000"), "reporting_type": "taxable",
     "description": "GST on taxable importations of goods (BAS label 7A / deferred GST)."},
]


async def _ensure_codes(
    session: AsyncSession,
    company_id: uuid.UUID,
    rows: list[dict[str, object]],
) -> int:
    """Idempotent insert of tax-code rows keyed on (jurisdiction, code).

    Skips any (jurisdiction, code) pair that already exists active for this
    company. Returns the number of rows inserted.
    """
    existing = await session.execute(
        select(TaxCode.jurisdiction, TaxCode.code).where(
            TaxCode.company_id == company_id, TaxCode.archived_at.is_(None)
        )
    )
    have = {(j, c) for (j, c) in existing.all()}
    inserted = 0
    for row in rows:
        key = (row["jurisdiction"], row["code"])
        if key in have:
            continue
        session.add(TaxCode(company_id=company_id, **row))
        have.add(key)
        inserted += 1
    return inserted


async def ensure_international_seed(
    session: AsyncSession, company_id: uuid.UUID
) -> int:
    """Idempotent: ensure the curated international + AU-extended reference
    tax-code set exists for this company.

    Safe to run repeatedly — keys on (company_id, jurisdiction, code) among
    active rows. Existing AU starter codes (AU_SEED) are untouched. These
    rows are jurisdiction-tagged and do NOT surface in the AU app (the list
    endpoints default to the home jurisdiction).
    """
    inserted = await _ensure_codes(session, company_id, AU_EXTENDED_SEED)
    inserted += await _ensure_codes(session, company_id, INTERNATIONAL_SEED)
    await session.commit()
    return inserted


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    jurisdiction: str | None = "AU",
) -> list[TaxCode]:
    """Active tax codes for a company.

    ``jurisdiction`` defaults to 'AU' (the home jurisdiction) so the app
    only ever shows AU codes — the international reference set seeded by
    ``ensure_international_seed`` stays hidden from the UI. Pass
    ``jurisdiction=None`` to return every jurisdiction (engine/admin use).
    """
    stmt = (
        select(TaxCode)
        .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
    )
    if tenant_id is not None:
        stmt = stmt.where(TaxCode.tenant_id == tenant_id)
    if jurisdiction is not None:
        stmt = stmt.where(TaxCode.jurisdiction == jurisdiction)
    stmt = stmt.order_by(TaxCode.code)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> TaxCode | None:
    """Fetch a tax code by id.

    P0 cross-tenant leak fix: when ``tenant_id`` is supplied, the
    lookup is filtered by tenant — a foreign-tenant id returns
    ``None`` even if the row exists. The parameter is keyword-only
    and optional so existing internal callers keep working unchanged;
    the API layer always supplies it.
    """
    if tenant_id is None and company_id is None:
        return await session.get(TaxCode, tax_code_id)
    clauses = [TaxCode.id == tax_code_id]
    if tenant_id is not None:
        clauses.append(TaxCode.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(TaxCode.company_id == company_id)
    result = await session.execute(
        select(TaxCode).where(*clauses)
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
