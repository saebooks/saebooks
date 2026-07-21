"""Company creation + seat/edition-aware cap enforcement.

``ensure_seed_company`` is the idempotent boot-time seed (no cap
check — the seed is mandatory). ``create_company`` is the cap-aware
path every human-initiated company-creation flow must go through:
CLI command, admin UI, import routines.

Company caps are always hard (CHARTER §12.3). Offline's "soft cap"
only covers admin seats, not companies.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.services import business_identifiers as biz_ident
from saebooks.services import change_log as change_log_svc
from saebooks.services.control_accounts import default_ap_code, default_ar_code
from saebooks.services.licence import check_company, resolve_licence


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value."""

    def __init__(self, current: Company) -> None:
        super().__init__(
            f"Company {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


# Fixed month lengths for fin-year-start-day validation. February is
# deliberately capped at 28, not 29 — day=29/30/31 with month=2 is a
# leap-year-only ambiguity and is rejected outright rather than silently
# clamped, per the period-picker engine spec (2026-07-21): an explicit 422
# is clearer than year-to-year drift in what "this FY" means.
_MAX_DAY_FOR_MONTH: dict[int, int] = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def validate_fin_year_start_day(month: int, day: int) -> None:
    """Raise ``ValueError`` if ``day`` can never occur in ``month``.

    Shared by the ``CompanyCreate``/``CompanyUpdate`` schema-layer
    validators (primary gate, field-scoped 422) and this module's
    ``create_company``/``update`` (belt-and-braces, same trust-but-verify
    posture as the existing 1-31 range checks).
    """
    max_day = _MAX_DAY_FOR_MONTH.get(month)
    if max_day is not None and day > max_day:
        raise ValueError(
            f"fin_year_start_day {day} is not valid for fin_year_start_month "
            f"{month} (max {max_day})"
        )


_COMPANY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "legal_name",
    "trading_name",
    "abn",
    "acn",
    "base_currency",
    "fin_year_start_month",
    "fin_year_start_day",
    "audit_mode",
    "tax_registered",
    "gst_effective_date",
    "psi_status",
    "writeoff_mode",
    "writeoff_threshold_days",
    "recovery_mode",
    "bad_debt_recovery_account",
    "ar_control_account_code",
    "ap_control_account_code",
    "asset_disposal_gain_account_code",
    "asset_disposal_loss_account_code",
    "lifecycle_status",
    "industry_code",
    "costing_method",
    "phone",
    "email",
    "website",
    "default_payment_terms",
    "bank_name",
    "bank_bsb",
    "bank_account_number",
    "bank_account_name",
    "payment_terms_text",
    "terms_url",
    "version",
    "created_at",
    "archived_at",
)


def _serialise(company: Company) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload (excludes encrypted SISS fields)."""
    from datetime import date as _date
    from datetime import datetime as _dt

    data: dict[str, Any] = {}
    for key in _COMPANY_COLUMNS:
        val = getattr(company, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (_dt, _date)):
            val = val.isoformat()
        data[key] = val
    return data


class CompanyCapExceeded(Exception):
    """Raised by ``create_company`` when the active edition is at its cap.

    The message is customer-facing — router handlers can surface it
    verbatim on a flash message or upgrade CTA.
    """

    def __init__(self, *, edition: str, limit: int, current: int) -> None:
        self.edition = edition
        self.limit = limit
        self.current = current
        super().__init__(
            f"Company cap reached for the {edition} edition "
            f"({current} of {limit} companies). "
            "Upgrade to add another."
        )


async def count_active_companies(session: AsyncSession) -> int:
    """Non-archived companies count against the edition cap."""
    stmt = select(func.count(Company.id)).where(Company.archived_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


async def _set_company_registry_id(
    session: AsyncSession, company: Company, scheme: str, value: str | None
) -> None:
    """Route a legacy AU registry field (``abn``/``acn``) onto its business
    identifier scheme (``au_abn``/``au_acn``).

    The physical ``companies.abn`` (0204) and ``companies.acn`` (0205) columns
    were dropped; ``business_identifiers`` is the single source of truth. A
    non-empty value upserts the scheme row; an empty string clears it. Reloads
    the company's cached ``identifiers`` collection so the ``Company.abn`` /
    ``Company.acn`` hybrids re-read the new value (change_log serialisation,
    CompanyOut, etc.). The caller owns the surrounding transaction / commit.
    """
    # A freshly-constructed company has no id until it is flushed (the uuid
    # primary-key default is applied at INSERT time), and the identifier's
    # company_id / the tenant-coherence trigger need it. Flush so it exists.
    if company.id is None:
        await session.flush()
    cleaned = (value or "").strip()
    if cleaned:
        # enforce_unique: the sanctioned company write path enforces
        # per-tenant value uniqueness for the EE registry schemes
        # (registrikood/KMV) — a duplicate raises DuplicateIdentifier ->
        # 409. Direct biz_ident.upsert callers (test fixtures, registry
        # sync) stay unconstrained (the primitive's default).
        await biz_ident.upsert(
            session,
            company.id,
            scheme,
            cleaned,
            tenant_id=company.tenant_id,
            enforce_unique=True,
        )
    else:
        existing = await biz_ident.get(session, company.id, scheme)
        if existing is not None:
            await session.delete(existing)
    await session.flush()
    # Awaited reload (NOT session.expire): the ``Company.abn`` hybrid is read
    # synchronously downstream (change_log ``_serialise``, CompanyOut). With
    # expire_on_commit=False an expired relationship would trigger a lazy load
    # from that sync context and raise MissingGreenlet; refreshing here leaves
    # the collection loaded and current.
    await session.refresh(company, ["identifiers"])


async def ensure_seed_company(session: AsyncSession) -> Company:
    """Idempotent: create the default company from env if none exists."""
    # This is a boot-time/CLI seed helper (seed_dev, seed_cashbook_demo,
    # load_au_coa, the test suite) — never called from a live per-request
    # session, so it is always safe to stamp the tenant here. Without this,
    # the read below silently returns zero rows (FORCE RLS filters out
    # every row when app.current_tenant is unset) and the write below
    # raises "new row violates row-level security policy" the moment the
    # runtime engine points at the NOBYPASSRLS saebooks_app role instead
    # of the owner role — see saebooks/db.py::_runtime_database_url and
    # saebooks/api/v1/deps.py's after_begin listener, which is what
    # actually issues ``SET LOCAL app.current_tenant`` once this is set.
    # setdefault (not unconditional assignment) so a caller that already
    # bound a different tenant onto this same session is not clobbered.
    session.info.setdefault(
        "tenant_id", "00000000-0000-0000-0000-000000000001"
    )
    name = settings.seed_company_name or "Default Company"
    result = await session.execute(
        select(Company).where(Company.name == name, Company.archived_at.is_(None))
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    # The core Company model now defaults jurisdiction/currency/template to
    # the neutral "XX"/"XXX"/"xx/default" sentinels, so the seed company must
    # state its jurisdiction explicitly (from config, default AU) — otherwise
    # Richard's home books would seed as a chart-less XX company.
    seed_jurisdiction = settings.seed_company_jurisdiction
    company = Company(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name=name,
        legal_name=settings.seed_company_legal_name or None,
        trading_name=settings.seed_company_trading_name or None,
        base_currency=settings.seed_company_base_currency,
        fin_year_start_month=settings.seed_company_fin_year_start_month,
        jurisdiction=seed_jurisdiction,
        coa_template_key=f"{seed_jurisdiction.lower()}/default",
    )
    session.add(company)
    await _set_company_registry_id(
        session, company, "au_abn", settings.seed_company_abn or None
    )
    await _set_company_registry_id(
        session, company, "au_acn", settings.seed_company_acn or None
    )
    await session.commit()
    await session.refresh(company)
    return company


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_company(
    session: AsyncSession,
    *,
    name: str,
    legal_name: str | None = None,
    trading_name: str | None = None,
    abn: str | None = None,
    acn: str | None = None,
    base_currency: str = "XXX",
    fin_year_start_month: int = 7,
    fin_year_start_day: int = 1,
    jurisdiction: str = "XX",
    coa_template_key: str = "xx/default",
    registrikood: str | None = None,
    kmv_number: str | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> Company:
    """Create a new company, enforcing the edition cap.

    Raises ``CompanyCapExceeded`` when the active edition is already at its
    company cap. ``jurisdiction`` / ``coa_template_key`` / EE-field format
    validation happens at the schema layer (``CompanyCreate``) before this
    is called — this function trusts its caller. Raises
    ``business_identifiers.DuplicateIdentifier`` if ``registrikood`` /
    ``kmv_number`` is already held by another company in the tenant.
    """
    licence = resolve_licence()
    current = await count_active_companies(session)
    check = check_company(licence.edition, current)
    if check.blocked:
        assert check.limit is not None  # blocked only happens when bounded
        raise CompanyCapExceeded(
            edition=licence.edition,
            limit=check.limit,
            current=current,
        )

    company = Company(
        tenant_id=tenant_id,
        name=name,
        legal_name=legal_name,
        trading_name=trading_name,
        base_currency=base_currency,
        fin_year_start_month=fin_year_start_month,
        fin_year_start_day=fin_year_start_day,
        jurisdiction=jurisdiction,
        coa_template_key=coa_template_key,
        version=1,
    )
    session.add(company)
    # Full-attempt atomicity (donor round 2/3): the Company row must not be
    # committed BEFORE its chart template is applied, or a failure applying
    # the template would leave an orphaned, chart-less company already
    # durable (jurisdiction/coa_template_key are immutable via PATCH) and
    # consuming an edition cap slot. Write the registry identifiers, flush
    # (not commit) so the row gets its id, apply the template, commit once.
    # A DuplicateIdentifier from the EE registry pre-check, or any applier
    # failure, rolls the whole attempt back. au/default is skipped here (its
    # chart is loaded by the seed script / CLI, unchanged); every other key
    # dispatches through the generic templates registry so a bad key fails
    # loudly instead of persisting a chart-less company. Note the EE applier
    # (chart_ee) commits internally — by the time apply_template returns for
    # ee/default the company + chart + identifiers are already durable, so a
    # failure in the trailing commit has nothing left to roll back.
    try:
        await _set_company_registry_id(session, company, "au_abn", abn)
        await _set_company_registry_id(session, company, "au_acn", acn)
        await _set_company_registry_id(session, company, "ee_regcode", registrikood)
        await _set_company_registry_id(session, company, "ee_vat", kmv_number)
        await session.flush()
        if coa_template_key != "au/default":
            from saebooks.services.templates import apply_template

            await apply_template(session, company.id, coa_template_key)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    await session.refresh(company)
    return company


async def list_active(session: AsyncSession) -> list[Company]:
    """List all non-archived companies."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.name)
    )
    return list(result.scalars().all())


async def get(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    return await session.get(Company, company_id)


async def update(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str | None = None,
    legal_name: str | None = None,
    trading_name: str | None = None,
    abn: str | None = None,
    acn: str | None = None,
    registrikood: str | None = None,
    kmv_number: str | None = None,
    base_currency: str | None = None,
    fin_year_start_month: int | None = None,
    fin_year_start_day: int | None = None,
    audit_mode: str | None = None,
    tax_registered: bool | None = None,
    gst_effective_date: date | None = None,
    psi_status: str | None = None,
    writeoff_mode: str | None = None,
    writeoff_threshold_days: int | None = None,
    recovery_mode: str | None = None,
    bad_debt_recovery_account: str | None = None,
    ar_control_account_code: str | None = None,
    ap_control_account_code: str | None = None,
    asset_disposal_gain_account_code: str | None = None,
    asset_disposal_loss_account_code: str | None = None,
    lifecycle_status: str | None = None,
    industry_code: str | None = None,
    costing_method: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    website: str | None = None,
    default_payment_terms: str | None = None,
    bank_name: str | None = None,
    bank_bsb: str | None = None,
    bank_account_number: str | None = None,
    bank_account_name: str | None = None,
    payment_terms_text: str | None = None,
    terms_url: str | None = None,
    address: dict | None = None,
    expected_version: int | None = None,
    actor: str = "web",
) -> Company:
    """Update company metadata. Raises VersionConflict on stale If-Match."""
    company = await session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")

    if expected_version is not None and company.version != expected_version:
        raise VersionConflict(company)

    if name is not None:
        company.name = name.strip()
    if legal_name is not None:
        company.legal_name = legal_name.strip() or None
    if trading_name is not None:
        company.trading_name = trading_name.strip() or None
    if abn is not None:
        await _set_company_registry_id(session, company, "au_abn", abn)
    if acn is not None:
        await _set_company_registry_id(session, company, "au_acn", acn)
    # EE registry identifiers (registrikood -> ee_regcode, kmv_number ->
    # ee_vat). EE-only regardless of entry point — mirrors CompanyCreate's
    # model_validator guard. A blank value clears the identifier (schema
    # lets blanks through for exactly this). jurisdiction is immutable via
    # PATCH, so company.jurisdiction is authoritative here. A duplicate
    # value raises business_identifiers.DuplicateIdentifier (pre-flush) ->
    # 409 at the router.
    if registrikood is not None:
        if company.jurisdiction != "EE":
            raise ValueError("registrikood can only be set on an EE company")
        await _set_company_registry_id(session, company, "ee_regcode", registrikood)
    if kmv_number is not None:
        if company.jurisdiction != "EE":
            raise ValueError("kmv_number can only be set on an EE company")
        await _set_company_registry_id(session, company, "ee_vat", kmv_number)
    if base_currency is not None:
        company.base_currency = base_currency.strip().upper()
    if fin_year_start_month is not None:
        if not 1 <= fin_year_start_month <= 12:
            raise ValueError("fin_year_start_month must be 1–12")
        company.fin_year_start_month = fin_year_start_month
    if fin_year_start_day is not None:
        if not 1 <= fin_year_start_day <= 31:
            raise ValueError("fin_year_start_day must be 1–31")
        company.fin_year_start_day = fin_year_start_day
    if fin_year_start_month is not None or fin_year_start_day is not None:
        # Cross-check the FINAL resolved state, not just the field(s) this
        # call touched -- e.g. a lone month=2 PATCH against a company whose
        # day is already 30 (set while month was, say, 4) must be caught
        # too, not just the reverse (lone day=30 PATCH against month=2).
        validate_fin_year_start_day(
            company.fin_year_start_month, company.fin_year_start_day
        )
    if audit_mode is not None:
        # CHARTER §7.2 vocabulary (Wave C, migration 0185 relabelled the
        # legacy {immutable, mutable, draft} column values onto this set:
        # mutable->open, draft->hybrid, see that migration's docstring for
        # the mapping rationale). Non-immutable values are the
        # extended_audit_modes paid feature (Offline+, CHARTER §12.1) — the
        # tier gate lives in the API route (api/v1/companies.py, via
        # require_feature_inline), not here, so this service function stays
        # usable from tests/CLI/seed scripts without a Request. Enforcement
        # of what mode actually governs a posted-entry EDIT is a separate,
        # fail-safe check in services/journal.py's effective_audit_mode() —
        # writing a non-immutable value onto a below-tier company is
        # harmless because that check re-derives entitlement itself and
        # ignores the stored value when the caller's tier lacks
        # FLAG_EXTENDED_AUDIT_MODES.
        valid_modes = {"immutable", "open", "hybrid"}
        if audit_mode not in valid_modes:
            raise ValueError(f"audit_mode must be one of: {', '.join(sorted(valid_modes))}")
        company.audit_mode = audit_mode
    if tax_registered is not None:
        company.tax_registered = tax_registered
    if gst_effective_date is not None:
        company.gst_effective_date = gst_effective_date
    if psi_status is not None:
        valid = {"yes", "no", "unsure"}
        if psi_status not in valid:
            raise ValueError(f"psi_status must be one of: {sorted(valid)}")
        company.psi_status = psi_status
    if writeoff_mode is not None:
        valid_wm = {"review", "auto", "manual"}
        if writeoff_mode not in valid_wm:
            raise ValueError(f"writeoff_mode must be one of: {sorted(valid_wm)}")
        company.writeoff_mode = writeoff_mode
    if writeoff_threshold_days is not None:
        if writeoff_threshold_days <= 0:
            raise ValueError("writeoff_threshold_days must be a positive integer")
        company.writeoff_threshold_days = writeoff_threshold_days
    if recovery_mode is not None:
        valid_rm = {"smart_prompt", "manual", "reopen"}
        if recovery_mode not in valid_rm:
            raise ValueError(f"recovery_mode must be one of: {sorted(valid_rm)}")
        company.recovery_mode = recovery_mode
    if bad_debt_recovery_account is not None:
        company.bad_debt_recovery_account = (
            bad_debt_recovery_account.strip() or None
        )
    # AR/AP control-account override (0198, Packet 4b) — same
    # optional-string-column pattern as bad_debt_recovery_account above.
    if ar_control_account_code is not None:
        company.ar_control_account_code = ar_control_account_code.strip() or None
    if ap_control_account_code is not None:
        company.ap_control_account_code = ap_control_account_code.strip() or None
    # Asset-disposal gain/loss account override (M1.5 P1 tail) — same
    # optional-string-column pattern as ar/ap control accounts above.
    if asset_disposal_gain_account_code is not None:
        company.asset_disposal_gain_account_code = (
            asset_disposal_gain_account_code.strip() or None
        )
    if asset_disposal_loss_account_code is not None:
        company.asset_disposal_loss_account_code = (
            asset_disposal_loss_account_code.strip() or None
        )
    if lifecycle_status is not None:
        valid_ls = {"active", "dormant", "in_liquidation", "deregistered"}
        if lifecycle_status not in valid_ls:
            raise ValueError(f"lifecycle_status must be one of: {sorted(valid_ls)}")
        company.lifecycle_status = lifecycle_status
    if industry_code is not None:
        company.industry_code = industry_code.strip() or None
    if costing_method is not None:
        valid_cm = {"weighted_average", "fifo", "quantity_only"}
        if costing_method not in valid_cm:
            raise ValueError(
                f"costing_method must be one of: {sorted(valid_cm)}"
            )
        company.costing_method = costing_method
    # Letterhead contact details + default payment terms (0171); remittance
    # fields (0168). PATCHing an empty string clears the column to NULL.
    if phone is not None:
        company.phone = phone.strip() or None
    if email is not None:
        company.email = email.strip() or None
    if website is not None:
        company.website = website.strip() or None
    if default_payment_terms is not None:
        company.default_payment_terms = default_payment_terms.strip() or None
    if bank_name is not None:
        company.bank_name = bank_name.strip() or None
    if bank_bsb is not None:
        company.bank_bsb = bank_bsb.strip() or None
    if bank_account_number is not None:
        company.bank_account_number = bank_account_number.strip() or None
    if bank_account_name is not None:
        company.bank_account_name = bank_account_name.strip() or None
    if payment_terms_text is not None:
        company.payment_terms_text = payment_terms_text.strip() or None
    if terms_url is not None:
        company.terms_url = terms_url.strip() or None
    if address is not None:
        company.address = address

    # Critic round 2/3: a PATCH that leaves AR/AP control accounts pointing
    # at the same GL account silently blends receivables and payables
    # into one balance (resolve_ar_code/resolve_ap_code both then
    # resolve identically, so every invoice's Dr-AR leg and every bill's
    # Cr-AP leg land in the same Account row). Checked against the FINAL
    # *resolved* state -- a NULL side falls back to AR_DEFAULT_CODE /
    # AP_DEFAULT_CODE exactly as resolve_ar_code/resolve_ap_code do at
    # posting time -- not just the two raw stored columns, so a partial
    # PATCH that sets only one side to a value the OTHER side would
    # implicitly resolve to (its unset default, or a value the DB
    # already holds) is caught too.
    effective_ar = company.ar_control_account_code or default_ar_code(company.jurisdiction)
    effective_ap = company.ap_control_account_code or default_ap_code(company.jurisdiction)
    if effective_ar == effective_ap:
        raise ValueError(
            "ar_control_account_code and ap_control_account_code must be "
            f"different accounts (both resolve to {effective_ar!r}) "
            "-- a shared control account would blend receivables and payables."
        )

    company.version = company.version + 1

    await session.flush()
    await session.refresh(company)
    await change_log_svc.append(
        session,
        entity="company",
        entity_id=company.id,
        op="update",
        actor=actor,
        payload=_serialise(company),
        version=company.version,
    )
    await session.commit()
    return company
