"""Tier-5 report tests — /api/v1/reports/bas_summary + /cashflow.

9 tests:
* test_bas_empty
* test_bas_taxable_sale
* test_bas_gst_free_sale
* test_bas_expense
* test_bas_net_gst_remit
* test_bas_tenant_isolation
* test_cashflow_empty
* test_cashflow_operating
* test_cashflow_investing
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token, DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import settings as settings_svc
pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _seed_gst_settings() -> None:
    """Configure the GST control account settings so the BAS report's
    ``_bas_gst_amounts`` aggregator finds the 1A/1B numbers.

    Round-2 audit fix #6 changed ``_bas_gst_amounts`` to read 1A/1B from
    the configured GST control accounts (``2-1310`` GST Collected and
    ``2-1330`` GST Paid). The aggregator returns (0, 0) when either
    setting is unset, regardless of any ``gst_amount`` stamped on the
    ledger.

    ``gst_auto_post`` is set to ``false`` here because the BAS tests
    include the GST control account leg explicitly in their JE payloads
    (the API's schema-level balance validator rejects unbalanced lines
    before the service-layer auto-poster can supply them). If auto-post
    ran on top of the explicit leg, the lines on INCOME/EXPENSE with
    ``gst_amount`` would cause a duplicate GST posting.

    AU CoA seed (``load_au_coa``) already creates the 2-1310 and 2-1330
    accounts; this fixture only wires the settings to point at them.
    Idempotent — settings.set() upserts.
    """
    async with AsyncSessionLocal() as session:
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "false")


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def gl_accounts() -> dict[str, str]:
    """Return one account ID per relevant AccountType, scoped to the seed
    company + DEFAULT_TENANT_ID. Also exposes the GST control accounts
    under the keys ``GST_COLLECTED`` and ``GST_PAID`` (codes 2-1310 /
    2-1330) so BAS tests can include those legs explicitly — the API's
    schema-level balance validator rejects unbalanced lines BEFORE the
    service-layer auto-poster can supply them.

    Filtering by both tenant_id and company_id is critical: prior tests
    (e.g. ``test_bas_tenant_isolation``) create accounts in foreign
    tenants/companies which would otherwise be selected by an unscoped
    ``LIMIT 1`` and then rejected by the API's tenant guard on the JE
    POST path (HTTP 422 "Account(s) do not belong to this tenant").
    """
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert company is not None, "Test DB has no seeded company"
        result: dict[str, str] = {}
        for at in (
            AccountType.INCOME,
            AccountType.EXPENSE,
            AccountType.ASSET,
            AccountType.LIABILITY,
            AccountType.EQUITY,
        ):
            row = (
                await session.execute(
                    select(Account)
                    .where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                        Account.tenant_id == DEFAULT_TENANT_ID,
                        Account.company_id == company.id,
                    )
                    .order_by(Account.code)
                    .limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
        # GST control accounts (codes 2-1310 / 2-1330) seeded by AU CoA.
        for key, code in (("GST_COLLECTED", "2-1310"), ("GST_PAID", "2-1330")):
            row = (
                await session.execute(
                    select(Account).where(
                        Account.archived_at.is_(None),
                        Account.code == code,
                        Account.tenant_id == DEFAULT_TENANT_ID,
                        Account.company_id == company.id,
                    )
                )
            ).scalars().first()
            assert row is not None, f"Seed AU CoA missing {code}"
            result[key] = str(row.id)
    return result


@pytest.fixture
async def tax_codes() -> dict[str, str]:
    """Return tax code IDs keyed by reporting_type from seeded AU tax codes.

    Scoped to the seed company + DEFAULT_TENANT_ID so the JE create path's
    tenant guard accepts them (TaxCode is per-company, like Account).
    """
    async with AsyncSessionLocal() as session:
        seed_company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert seed_company is not None
        gst_row = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "GST",
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == seed_company.id,
                )
            )
        ).scalars().first()
        fre_row = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "FRE",
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == seed_company.id,
                )
            )
        ).scalars().first()
        assert gst_row is not None, "Seed tax code GST not found"
        assert fre_row is not None, "Seed tax code FRE not found"
        return {
            "taxable": str(gst_row.id),
            "gst_free": str(fre_row.id),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_and_post_je(
    client: AsyncClient,
    entry_date: str,
    lines: list[dict],
) -> dict:
    """Create a DRAFT journal entry then transition it to POSTED via the
    dedicated ``/post`` endpoint. Return the posted body.

    Critical: PATCH /journal_entries/{id} with ``status=POSTED`` bypasses
    ``services.journal.post()`` — it just flips the column. That skips
    ``auto_post_gst_lines`` and the balance/period-lock checks. For BAS
    tests (post fix #6) that read 1A/1B from the GST control accounts,
    we MUST go through ``POST /{id}/post`` so auto-post actually fires
    on lines carrying ``gst_amount``.
    """
    r = await client.post(
        "/api/v1/journal_entries",
        json={
            "entry_date": entry_date,
            "narration": "Test BAS/cashflow entry",
            "lines": lines,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    je_id = body["id"]
    version = body["version"]

    r2 = await client.post(
        f"/api/v1/journal_entries/{je_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# BAS tests
# ---------------------------------------------------------------------------


async def test_bas_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs in range → all BAS fields zero."""
    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "1997-01-01", "to_date": "1997-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["from_date"] == "1997-01-01"
    assert body["to_date"] == "1997-12-31"
    assert body["g1_total_sales"] == 0.0
    assert body["g2_export_sales"] == 0.0
    assert body["g3_other_gst_free_sales"] == 0.0
    assert body["g10_capital_acquisitions"] == 0.0
    assert body["g11_other_acquisitions"] == 0.0
    assert body["label_1a_gst_on_sales"] == 0.0
    assert body["label_1b_gst_on_purchases"] == 0.0
    assert body["net_gst"] == 0.0
    # remit_or_refund can be either when net_gst == 0; just assert it's present
    assert body["remit_or_refund"] in ("REMIT", "REFUND")


async def test_bas_taxable_sale(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE crediting a taxable INCOME account → G1 and 1A computed."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Round-2 audit fix #6: 1A is read at report time from the GST
    # Collected control account (2-1310). The JE includes that leg
    # explicitly — auto-post is disabled in tests (fixture above) and
    # the API's schema-level balance validator would reject the JE if
    # we relied on auto-post to supply the third line.
    gst_collected_id = gl_accounts["GST_COLLECTED"]
    await _create_and_post_je(
        api_client,
        "2027-01-15",
        lines=[
            {
                "account_id": asset_id,
                "debit": "1100.00",
                "credit": "0",
            },
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "1000.00",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_collected_id,
                "debit": "0",
                "credit": "100.00",
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-01-01", "to_date": "2027-01-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["g1_total_sales"] >= 1000.0, f"Expected g1 >= 1000, got {body['g1_total_sales']}"
    # 1A = sum of gst_amount on income lines (post-fix #6).
    assert body["label_1a_gst_on_sales"] >= 100.0, (
        f"Expected 1a >= 100, got {body['label_1a_gst_on_sales']}"
    )
    assert body["g3_other_gst_free_sales"] == 0.0


async def test_bas_gst_free_sale(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE crediting a gst_free INCOME account → G3 populated, G1 and 1A unaffected."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    fre_id = tax_codes["gst_free"]

    amount = "500.00"
    await _create_and_post_je(
        api_client,
        "2027-02-10",
        lines=[
            {
                "account_id": asset_id,
                "debit": amount,
                "credit": "0",
            },
            {
                "account_id": income_id,
                "debit": "0",
                "credit": amount,
                "tax_code_id": fre_id,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-02-01", "to_date": "2027-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["g3_other_gst_free_sales"] >= 500.0, (
        f"Expected g3 >= 500, got {body['g3_other_gst_free_sales']}"
    )
    # GST-free sales contribute nothing to 1A by design — they don't
    # carry a gst_amount.  Round-2 audit fix #6: 1A is now sum of
    # gst_amount on income lines, not g1*10%. Asserting "1A==G1*10%"
    # would re-encode the bug.


async def test_bas_expense(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE debiting a taxable EXPENSE account → G11 and 1B computed."""
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Round-2 audit fix #6: 1B is read at report time from the GST Paid
    # control account (2-1330). JE includes that leg explicitly; the
    # expense line carries the NET amount and the asset/bank Cr the
    # gross. G11 sums net debit on EXPENSE lines = $1000.
    gst_paid_id = gl_accounts["GST_PAID"]
    await _create_and_post_je(
        api_client,
        "2027-03-05",
        lines=[
            {
                "account_id": expense_id,
                "debit": "1000.00",
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_paid_id,
                "debit": "100.00",
                "credit": "0",
            },
            {
                "account_id": asset_id,
                "debit": "0",
                "credit": "1100.00",
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-03-01", "to_date": "2027-03-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # G11 sums the NET (ex-GST) debit on EXPENSE lines — gst_amount is a
    # sidecar that becomes a separate Dr to 2-1330 via auto-post and does
    # NOT inflate G11. Expense leg here was Dr $1000 net + $100 gst_amount,
    # so G11 = 1000 (not 1100 as in the pre-fix-#6 test).
    assert body["g11_other_acquisitions"] >= 1000.0, (
        f"Expected g11 >= 1000, got {body['g11_other_acquisitions']}"
    )
    # 1B = net Dr on the GST Paid control account (post-fix #6); auto-post
    # added that leg from the stamped gst_amount.
    assert body["label_1b_gst_on_purchases"] >= 100.0, (
        f"Expected 1b >= 100, got {body['label_1b_gst_on_purchases']}"
    )


async def test_bas_net_gst_remit(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """1A > 1B → remit_or_refund == REMIT."""
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    gst_collected_id = gl_accounts["GST_COLLECTED"]
    gst_paid_id = gl_accounts["GST_PAID"]
    # Large taxable sale — explicit Cr 2-1310 leg supplies the $500 of
    # GST collected that 1A reads at report time.
    await _create_and_post_je(
        api_client,
        "2027-04-15",
        lines=[
            {"account_id": asset_id, "debit": "5500.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "5000.00",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_collected_id,
                "debit": "0",
                "credit": "500.00",
            },
        ],
    )
    # Small taxable expense — explicit Dr 2-1330 leg supplies the $10
    # of GST paid that 1B reads at report time.
    await _create_and_post_je(
        api_client,
        "2027-04-20",
        lines=[
            {
                "account_id": expense_id,
                "debit": "100.00",
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {"account_id": gst_paid_id, "debit": "10.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "110.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-04-01", "to_date": "2027-04-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["label_1a_gst_on_sales"] > body["label_1b_gst_on_purchases"], (
        "1A should exceed 1B in this scenario"
    )
    assert body["net_gst"] > 0.0
    assert body["remit_or_refund"] == "REMIT"


async def test_bas_tenant_isolation(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """Tenant B cannot see tenant A's taxable sales in the BAS report."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Post a JE under the default tenant (A). 3-line shape: Asset gross
    # / Income net / GST control. (Auto-post is off; explicit leg
    # required for the schema-level balance check to pass.)
    gst_collected_id = gl_accounts["GST_COLLECTED"]
    await _create_and_post_je(
        api_client,
        "2027-05-10",
        lines=[
            {"account_id": asset_id, "debit": "8554.70", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "7777.00",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_collected_id,
                "debit": "0",
                "credit": "777.70",
            },
        ],
    )

    # Query BAS as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/bas_summary",
            params={"from_date": "2027-05-01", "to_date": "2027-05-31"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B has no company → 404, which proves isolation.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        assert body["g1_total_sales"] < 7777.0, (
            "Tenant B should not see tenant A's taxable sales in BAS"
        )


async def test_bas_mid_quarter_registration_split(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """Mid-quarter GST registration: pre-reg sales in G1 but excluded from 1A.

    Scenario (HOBB-3 fix):
      Quarter     2027-07-01 to 2027-09-30
      Registration effective 2027-08-01
      Pre-reg sale  2027-07-15  AUD 2000 taxable
      Post-reg sale 2027-08-10  AUD 3000 taxable
    Expected:
      g1_total_sales = 5000  (all sales disclosed)
      g1_pre_registration = 2000
      g1_post_registration = 3000
      label_1a_gst_on_sales = 300  (3000 x 10% — pre-reg excluded)
    """
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    gst_collected_id = gl_accounts["GST_COLLECTED"]
    # Pre-registration sale (2027-07-15). The GST control leg is posted
    # in the ledger; only the BAS 1A label clamps to post-registration
    # (per ATO mid-period registration rules; ``_bas_gst_amounts`` is
    # called with from_date=registration_effective_date when split).
    await _create_and_post_je(
        api_client,
        "2027-07-15",
        lines=[
            {"account_id": asset_id, "debit": "2200.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "2000.00",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_collected_id,
                "debit": "0",
                "credit": "200.00",
            },
        ],
    )

    # Post-registration sale (2027-08-10) — drives 1A entirely.
    await _create_and_post_je(
        api_client,
        "2027-08-10",
        lines=[
            {"account_id": asset_id, "debit": "3300.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "3000.00",
                "tax_code_id": gst_id,
            },
            {
                "account_id": gst_collected_id,
                "debit": "0",
                "credit": "300.00",
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={
            "from_date": "2027-07-01",
            "to_date": "2027-09-30",
            "registration_effective_date": "2027-08-01",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["registration_effective_date"] == "2027-08-01"
    assert body["g1_total_sales"] >= 5000.0, f"g1_total_sales={body['g1_total_sales']}"
    assert body["g1_pre_registration"] >= 2000.0, (
        f"g1_pre_registration={body['g1_pre_registration']}"
    )
    assert body["g1_post_registration"] >= 3000.0, (
        f"g1_post_registration={body['g1_post_registration']}"
    )
    # 1A must be based only on post-registration sales (3000 x 10% = 300)
    assert body["label_1a_gst_on_sales"] == pytest.approx(
        body["g1_post_registration"] * 0.10, abs=0.02
    ), f"1A should be post-reg G1 x 10%, got {body['label_1a_gst_on_sales']}"
    assert body["label_1a_gst_on_sales"] < body["g1_total_sales"] * 0.10, (
        "1A must be less than it would be if all sales were taxable"
    )


# ---------------------------------------------------------------------------
# Round-2 audit fix #6: 1A/1B derive from journal_lines.gst_amount,
# not g1/10 or g11/11. Critics 07 + 19 reported $141.82 understated 1B
# in a scenario where g11/11 reverse-calculated GST diverged from the
# actual GST Paid control account. This guard pins the new behaviour.
# ---------------------------------------------------------------------------


async def test_bas_1b_matches_actual_gst_on_purchases_not_g11_div_11(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """A $1000 ex-GST expense with $100 of actual GST must drive
    1B = $100, NOT (g11 = 1100) / 11 = $100.

    Test scenario explicitly designed to expose drift: book a $1000
    ex-GST + $100 GST expense as a journal entry. Confirm:

      g11 = 1100 (ex-GST + GST = inclusive purchase total)
      1B  = 100  (actual GST stamped on the expense line)

    In the bug shape, 1B would still be (1100 / 11).quantize = 100.00
    by coincidence — so this test alone doesn't catch the bug. The
    follow-up ``test_bas_1b_matches_when_g11_diverges_from_gst`` posts
    a JE where the GST is intentionally different from g11/11, which
    exercises the drift path the critics found.
    """
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Explicit GST Paid leg: 1B reads 2-1330 net Dr = $100.
    gst_paid_id = gl_accounts["GST_PAID"]
    await _create_and_post_je(
        api_client,
        "2028-01-15",
        lines=[
            {
                "account_id": expense_id,
                "debit": "1000.00",
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {"account_id": gst_paid_id, "debit": "100.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1100.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2028-01-01", "to_date": "2028-01-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 1B equals the GST Paid control posting, full stop. Not g11/11.
    assert body["label_1b_gst_on_purchases"] == pytest.approx(100.00, abs=0.01), (
        f"Expected 1B=100.00 from ledger gst_amount, got "
        f"{body['label_1b_gst_on_purchases']}"
    )


async def test_bas_1b_matches_when_g11_diverges_from_gst(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """Critical fix #6 scenario: when the expense total and the actual
    GST stamped on the line don't satisfy ``gst = total/11`` (because
    of a margin scheme, manual adjustment, or partial-GST line), 1B
    must still come from the ledger.

    JE: $1000 ex-GST expense + $50 GST (margin scheme — only 5% of
    total is GST, not 10%). Pre-fix: 1B = $1050/11 = $95.45 (WRONG).
    Post-fix: 1B = $50 (from gst_amount). This is the exact bug critics
    07 + 19 found.
    """
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Margin scheme: GST is 5% not 10% of total. Explicit Dr 2-1330 $50
    # supplies 1B. G11 still sums expense net debit ($1000); the divergence
    # between g11/11 = 95.45 and the actual 1B = 50.00 is the bug shape.
    gst_paid_id = gl_accounts["GST_PAID"]
    await _create_and_post_je(
        api_client,
        "2028-02-15",
        lines=[
            {
                "account_id": expense_id,
                "debit": "1000.00",
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {"account_id": gst_paid_id, "debit": "50.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1050.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2028-02-01", "to_date": "2028-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 1B is what the ledger says, NOT g11/11.
    assert body["label_1b_gst_on_purchases"] == pytest.approx(50.00, abs=0.01), (
        f"Pre-fix bug: 1B was g11/11 = 95.45. Post-fix #6: 1B must "
        f"equal the stamped gst_amount of 50.00. Got "
        f"{body['label_1b_gst_on_purchases']}"
    )
    # g11/11 for sanity — the bug path would have returned 1050/11 = 95.45.
    assert body["label_1b_gst_on_purchases"] != pytest.approx(95.45, abs=0.05), (
        "1B is still being reverse-calculated from g11/11 — fix #6 regressed."
    )


# ---------------------------------------------------------------------------
# Cashflow tests
# ---------------------------------------------------------------------------


async def test_cashflow_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs in range → net_change == 0."""
    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "1996-01-01", "to_date": "1996-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["from_date"] == "1996-01-01"
    assert body["to_date"] == "1996-12-31"
    assert body["operating"]["net_profit"] == 0.0
    assert body["operating"]["total_operating"] == 0.0
    assert body["operating"]["adjustments"] == []
    assert body["investing"]["total_investing"] == 0.0
    assert body["financing"]["total_financing"] == 0.0
    assert body["net_change"] == 0.0
    assert abs(body["closing_cash"] - body["opening_cash"]) < 0.01


async def test_cashflow_operating(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
) -> None:
    """Income and expense JEs drive net_profit in the operating section.

    Uses a clean month (2030-06) outside the windows that other BAS tests
    above post into — earlier tests in this file post in 2028-01 and would
    otherwise leak into this period's net_profit.
    """
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Income 3000
    await _create_and_post_je(
        api_client,
        "2030-06-10",
        lines=[
            {"account_id": asset_id, "debit": "3000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "3000.00"},
        ],
    )
    # Expense 1000
    await _create_and_post_je(
        api_client,
        "2030-06-20",
        lines=[
            {"account_id": expense_id, "debit": "1000.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1000.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "2030-06-01", "to_date": "2030-06-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["operating"]["net_profit"] >= 2000.0, (
        f"Expected net_profit >= 2000, got {body['operating']['net_profit']}"
    )
    assert body["operating"]["total_operating"] == body["operating"]["net_profit"]


async def test_cashflow_investing(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
) -> None:
    """A non-cash ASSET debit shows as an outflow in the investing section."""
    # We need a non-cash asset account — use the seeded fixed-asset or any
    # ASSET account whose name/code does NOT contain "cash" or "bank".
    # Filter by the seed company's tenant so earlier tests' foreign-tenant
    # accounts (e.g. test_bas_tenant_isolation) are excluded — picking one
    # of those would 422 on the JE POST with "do not belong to this tenant".
    async with AsyncSessionLocal() as session:
        seed_company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert seed_company is not None
        fixed_asset_row = (
            await session.execute(
                select(Account)
                .where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.company_id == seed_company.id,
                )
                .order_by(Account.code)
            )
        ).scalars().all()

    # Find one that is not a cash/bank account
    non_cash_asset = None
    for acct in fixed_asset_row:
        name_lower = acct.name.lower()
        code_lower = acct.code.lower()
        if not any(kw in name_lower or kw in code_lower for kw in ("cash", "bank")):
            non_cash_asset = str(acct.id)
            break

    if non_cash_asset is None:
        pytest.skip("No non-cash ASSET account found in test DB")

    income_id = gl_accounts[AccountType.INCOME.value]

    # DR non-cash asset, CR income (simulates asset purchase financed by
    # income). Clean period (2030-07) avoids overlap with earlier BAS
    # tests in this file that post into 2028-01/02 and would otherwise
    # leak into the investing-section asset_purchases bucket.
    await _create_and_post_je(
        api_client,
        "2030-07-05",
        lines=[
            {"account_id": non_cash_asset, "debit": "4000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "4000.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "2030-07-01", "to_date": "2030-07-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Asset purchase is an outflow → asset_purchases is negative (outflow convention)
    assert body["investing"]["asset_purchases"] <= -4000.0, (
        f"Expected asset_purchases <= -4000 (outflow), got {body['investing']['asset_purchases']}"
    )
    assert body["investing"]["total_investing"] <= -4000.0, (
        f"Expected total_investing <= -4000, got {body['investing']['total_investing']}"
    )
