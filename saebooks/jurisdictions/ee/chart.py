"""EE (Estonia) chart-of-accounts applier.

Provides the ``ee/default`` template applier that ``services.templates``
dispatches (M3). Lives in the EE jurisdiction package (not
``services/``) so the neutral core imports zero jurisdiction modules:
``jurisdictions/ee/__init__.py`` registers a lazy factory pointing here
via ``services.templates.register_template_applier``, and the dispatcher
resolves it through ``bootstrap.jurisdictions.ensure_loaded()`` (the
Job C registration-inversion shape). EE-specific data + compute lives
in this package, not inlined in the jurisdiction-neutral dispatcher or
hardcoded into core.

Data source: reference DB first (``ChartTemplate`` rows seeded from
``saebooks/seeds/jurisdictions/EE/chart_template.yaml`` via
``services.reference.loader``), falling back to an embedded snapshot of
the SAME 37 rows when ``ReferenceSession`` is not configured — the exact
convention ``tax_return_generator._fetch_box_definitions`` and
``payroll_ee._resolve_rates`` already use, and the one every test in this
repo actually exercises (``REFERENCE_DATABASE_URL`` is unset in the
standard test/CI harness). Keep this fallback in lock-step with the
yaml — it is not independently authoritative.

Unlike the AU path (``services.templates._apply_au_default``, which still
delegates to the legacy CSV-driven ``seed.load_au_coa``), this is the
first jurisdiction applier written *after* the CoA moved into
reference-DB-backed ``ChartTemplate`` rows — so it reads that table
directly rather than a CSV.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import ReferenceSession

# Importing ``identifiers`` activates the ee_regcode/ee_vat business-identifier
# validators (check_digit_valid) — the lazy "first EE dispatch" trigger,
# exactly how lv.tax importing lv.identifiers activates lv_pvn/lv_regnum.
from saebooks.jurisdictions.ee import (
    EE_AP_CONTROL_CODE,
    EE_AR_CONTROL_CODE,
)
from saebooks.jurisdictions.ee import (
    identifiers as _ee_identifiers,  # noqa: F401
)
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.reference.chart_template import ChartTemplate
from saebooks.services.tax_return_generator import _to_reference_jurisdiction


@dataclass(frozen=True)
class _EEChartRow:
    account_code: str
    account_name: str
    account_type: str
    default_tax_code: str | None
    display_order: int


# Lock-step copy of saebooks/seeds/jurisdictions/EE/chart_template.yaml
# (jurisdiction: EST) — 37 rows. Used only when the reference DB is not
# configured/seeded; see module docstring.
_EMBEDDED_FALLBACK: tuple[_EEChartRow, ...] = (
    _EEChartRow("1100", "Pank — arvelduskonto (Bank — operating)", "ASSET", None, 10),
    _EEChartRow("1110", "Pank — hoiukonto (Bank — savings)", "ASSET", None, 11),
    _EEChartRow("1200", "Ostjatega arveldused (Accounts Receivable)", "ASSET", None, 20),
    _EEChartRow("1300", "Varud (Inventory)", "ASSET", None, 30),
    _EEChartRow("1400", "Käibemaks (VAT) Receivable", "ASSET", None, 40),
    _EEChartRow("1500", "Ettemakstud kulud (Prepayments)", "ASSET", None, 50),
    _EEChartRow("1700", "Materiaalne põhivara — soetusmaksumus (Property, Plant & Equipment — at cost)", "ASSET", None, 70),
    _EEChartRow("1710", "Materiaalne põhivara — akumuleeritud kulum (Property, Plant & Equipment — accumulated depreciation)", "ASSET", None, 71),
    _EEChartRow("2100", "Hankijatega arveldused (Accounts Payable)", "LIABILITY", None, 100),
    _EEChartRow("2200", "Käibemaks (VAT) Payable", "LIABILITY", None, 110),
    _EEChartRow("2300", "Kinnipeetud tulumaks (Withheld Income Tax Payable — TSD)", "LIABILITY", None, 120),
    _EEChartRow("2310", "Sotsiaalmaks (Social Tax Payable)", "LIABILITY", None, 121),
    _EEChartRow("2320", "Töötuskindlustusmakse (Unemployment Insurance Payable)", "LIABILITY", None, 122),
    _EEChartRow("2330", "Kohustuslik kogumispension — II sammas (Mandatory Funded Pension — Pillar II Payable)", "LIABILITY", None, 123),
    _EEChartRow("2400", "Palgakohustused (Wages Payable)", "LIABILITY", None, 130),
    _EEChartRow("2500", "Ettevõtte tulumaks — jaotatud kasum (Corporate Income Tax Payable — Distributed Profit)", "LIABILITY", None, 140),
    _EEChartRow("2700", "Pangalaen (Bank Loan)", "LIABILITY", None, 160),
    _EEChartRow("3100", "Osa-/aktsiakapital (Share Capital)", "EQUITY", None, 200),
    _EEChartRow("3200", "Jaotamata kasum (eelmised perioodid) (Retained Earnings)", "EQUITY", None, 210),
    _EEChartRow("3300", "Aruandeaasta kasum/kahjum (Current Year Earnings)", "EQUITY", None, 220),
    _EEChartRow("4100", "Müügitulu — teenused (Sales — Services)", "INCOME", "STD", 300),
    _EEChartRow("4200", "Müügitulu — kaubad (Sales — Goods)", "INCOME", "STD", 310),
    _EEChartRow("4300", "Muud äritulud (Other Operating Income)", "OTHER_INCOME", "STD", 320),
    _EEChartRow("4400", "Eksport ja ühendusesisene käive (Export & Intra-Community Supply)", "INCOME", "ZERO_EXPORT", 325),
    _EEChartRow("4900", "Intressitulu (Interest Income)", "OTHER_INCOME", "NTR", 330),
    _EEChartRow("5100", "Müüdud kaupade kulu (Cost of Goods Sold)", "COST_OF_SALES", "INPUT_STD", 400),
    _EEChartRow("6100", "Palgakulu (Wages & Salaries)", "EXPENSE", "NTR", 500),
    _EEChartRow("6110", "Sotsiaalmaksu kulu (Social Tax Expense)", "EXPENSE", "NTR", 510),
    _EEChartRow("6120", "Töötuskindlustusmakse kulu — tööandja (Unemployment Insurance Expense — Employer)", "EXPENSE", "NTR", 511),
    _EEChartRow("6200", "Üür (Rent)", "EXPENSE", "INPUT_STD", 520),
    _EEChartRow("6300", "Kommunaalkulud (Utilities)", "EXPENSE", "INPUT_STD", 530),
    _EEChartRow("6400", "Side- ja internetikulud (Telephone & Internet)", "EXPENSE", "INPUT_STD", 540),
    _EEChartRow("6500", "Sõidukikulud (Motor Vehicle)", "EXPENSE", "INPUT_STD", 550),
    _EEChartRow("6600", "Pangakulud (Bank Fees)", "EXPENSE", "INPUT_EXEMPT", 560),
    _EEChartRow("6700", "Kulum (Depreciation)", "EXPENSE", "NTR", 570),
    _EEChartRow("6800", "Erisoodustused (Fringe Benefits — TSD Lisa 4)", "EXPENSE", "NTR", 580),
    _EEChartRow("6900", "Ettevõtte tulumaks — jaotatud kasum (Corporate Income Tax Expense — Distributed Profit)", "OTHER_EXPENSE", "NTR", 590),
)


async def _fetch_chart_rows() -> tuple[list[_EEChartRow], str]:
    """Return ``(rows, source)``; ``source`` is ``"reference_db"`` or
    ``"embedded_fallback"``. Same shape/convention as
    ``tax_return_generator._fetch_box_definitions``."""
    ref_code = _to_reference_jurisdiction("EE")
    if ReferenceSession is not None:
        async with ReferenceSession() as ref:
            result = await ref.execute(
                select(ChartTemplate)
                .where(ChartTemplate.jurisdiction == ref_code)
                .order_by(ChartTemplate.display_order)
            )
            rows = result.scalars().all()
        if rows:
            return (
                [
                    _EEChartRow(
                        account_code=r.account_code,
                        account_name=r.account_name,
                        account_type=r.account_type,
                        default_tax_code=r.default_tax_code,
                        display_order=r.display_order,
                    )
                    for r in rows
                ],
                "reference_db",
            )

    return list(_EMBEDDED_FALLBACK), "embedded_fallback"


async def apply_ee_chart_template(session: AsyncSession, company: Company) -> None:
    """Create ``company``'s accounts from the EE ('ee/default') chart
    template. Idempotent — an existing (company_id, code) is left alone,
    so re-applying inserts nothing new.

    Also sets ``company.ar_control_account_code`` /
    ``ap_control_account_code`` to the EE convention codes
    (``EE_AR_CONTROL_CODE`` / ``EE_AP_CONTROL_CODE``) when NOT already
    set — the AU-convention resolver defaults in
    ``services.control_accounts`` ("1-1200"/"2-1200") don't exist in an EE
    chart. An existing override (earlier wave, 0198) is left untouched, on
    the caller's own re-apply as much as a fresh apply.
    """
    rows, _source = await _fetch_chart_rows()

    existing = await session.execute(
        select(Account.code).where(Account.company_id == company.id)
    )
    existing_codes = {code for (code,) in existing.all()}

    for row in rows:
        if row.account_code in existing_codes:
            continue
        session.add(
            Account(
                company_id=company.id,
                tenant_id=company.tenant_id,
                code=row.account_code,
                name=row.account_name,
                account_type=AccountType(row.account_type),
                tax_code_default=row.default_tax_code,
                version=1,
            )
        )
        existing_codes.add(row.account_code)

    if not company.ar_control_account_code:
        company.ar_control_account_code = EE_AR_CONTROL_CODE
    if not company.ap_control_account_code:
        company.ap_control_account_code = EE_AP_CONTROL_CODE

    await session.commit()


async def known_chart_row_codes() -> set[str]:
    """The full set of account codes the current template source would
    create — used by tests to assert the created account set matches the
    reference template without hardcoding a row count."""
    rows, _source = await _fetch_chart_rows()
    return {row.account_code for row in rows}
