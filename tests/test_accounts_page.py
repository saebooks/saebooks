import os

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.postgres_only


async def test_accounts_list_renders(client: AsyncClient) -> None:
    r = await client.get("/accounts")
    assert r.status_code == 200
    body = r.text
    assert "Chart of accounts" in body
    assert "accounts" in body  # count shown but varies with DB state
    # Company name on the page is the seeded company (SEED_COMPANY_NAME env,
    # default 'Default Company') — not a hard-coded brand.
    expected_company = os.environ.get("SEED_COMPANY_NAME", "Default Company")
    assert expected_company in body
    # One of the seeded accounts should be present by code
    assert "1-1110" in body  # Bank (hyphenated per migration 0010)
    assert "Assets" in body
    assert "Liabilities" in body


async def test_accounts_list_has_create_form(client: AsyncClient) -> None:
    r = await client.get("/accounts")
    assert r.status_code == 200
    assert "New account" in r.text
    assert 'name="code"' in r.text


# NOTE: legacy /admin/settings HTML page removed in Cat-C rollup; re-add a
# /api/v1/admin/settings test when that endpoint is added.
