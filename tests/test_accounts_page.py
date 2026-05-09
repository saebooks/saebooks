from httpx import AsyncClient


async def test_accounts_list_renders(client: AsyncClient) -> None:
    r = await client.get("/accounts")
    assert r.status_code == 200
    body = r.text
    assert "Chart of accounts" in body
    assert "accounts" in body  # count shown but varies with DB state
    assert "Sauer Pty Ltd ATF Saueesti Trust" in body
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
