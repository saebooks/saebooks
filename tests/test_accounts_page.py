from httpx import AsyncClient


async def test_accounts_list_renders(client: AsyncClient) -> None:
    r = await client.get("/accounts")
    assert r.status_code == 200
    body = r.text
    assert "Chart of accounts" in body
    assert "124 accounts" in body
    assert "Sauer Pty Ltd ATF Saueesti Trust" in body
    # One of the seeded accounts should be present by code
    assert "11110" in body  # Bank
    assert "Assets" in body
    assert "Liabilities" in body


async def test_account_detail_stub(client: AsyncClient) -> None:
    r = await client.get("/accounts/some-id")
    assert r.status_code == 200
    assert "TODO" in r.text


async def test_admin_settings_page(client: AsyncClient) -> None:
    r = await client.get("/admin/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "base_currency" in r.text or "AUD" in r.text
