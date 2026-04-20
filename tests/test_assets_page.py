"""Router smoke tests for ``/assets``.

Exercises the full HTTP surface end-to-end against the real app:

- List page renders with status filters
- New-asset form renders with dropdowns populated from the seed
- Create → 303 redirect to detail page
- Detail page shows cost/NBV
- Edit page renders with the asset's values prefilled
- Depreciate POST bumps the cursor and redirects back to detail
- Archive POST redirects to list
- Dispose form + POST; disposed asset appears under ?status=disposed

All assets get ``FA-SMOKE-*`` codes so they can't collide with other tests.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient


async def _first_company_and_accounts(client: AsyncClient) -> dict[str, str]:
    """Pull the account IDs we need from the accounts list page.

    Parses the CoA list for account IDs rather than reaching into the DB,
    which keeps this an honest HTTP smoke test.
    """
    # The new-asset form embeds all account ids as <option value="uuid">,
    # which is the simplest way to grab one of each kind.
    r = await client.get("/assets/new")
    assert r.status_code == 200
    return {"html": r.text}


async def _create_asset_via_http(
    client: AsyncClient,
    *,
    name: str,
    cost: str = "1200.00",
    model: str = "asset_5_year_linear",
) -> str:
    """POST /assets and return the new asset's ID (extracted from the Location header)."""
    # Grab dropdown ids
    new_page = await client.get("/assets/new")
    assert new_page.status_code == 200
    body = new_page.text

    # Extract first id from cost_account_id select, accum dep select, etc.
    # All three use the same asset-accounts pool; for a smoke test any ID
    # from the pool is fine so long as it exists.
    def _first_option_value(html: str, select_name: str) -> str:
        idx = html.index(f'name="{select_name}"')
        # find the first <option value="..."> after that
        option_idx = html.index('<option value="', idx)
        start = option_idx + len('<option value="')
        end = html.index('"', start)
        return html[start:end]

    cost_id = _first_option_value(body, "cost_account_id")
    accum_id = _first_option_value(body, "accum_dep_account_id")
    dep_id = _first_option_value(body, "dep_expense_account_id")

    code = f"FA-SMOKE-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/assets",
        data={
            "code": code,
            "name": name,
            "description": "",
            "cost_account_id": cost_id,
            "accum_dep_account_id": accum_id,
            "dep_expense_account_id": dep_id,
            "depreciation_model_id": model,
            "purchase_date": "2026-04-01",
            "in_service_date": "2026-04-01",
            "cost": cost,
            "residual_value": "0",
            "serial_number": "",
            "manufacturer": "",
            "model_number": "",
            "location": "",
            "custody_person": "",
            "warranty_end": "",
            "purchase_contact_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, (resp.status_code, resp.text[:500])
    loc = resp.headers["location"]
    # /assets/<uuid>
    return loc.rsplit("/", 1)[-1]


async def test_list_page_renders(client: AsyncClient) -> None:
    r = await client.get("/assets")
    assert r.status_code == 200
    assert "Fixed assets" in r.text
    assert "+ New" in r.text


async def test_list_page_status_filters(client: AsyncClient) -> None:
    for s in ("active", "disposed", "archived"):
        r = await client.get(f"/assets?status={s}")
        assert r.status_code == 200
        assert "Fixed assets" in r.text


async def test_new_form_renders_with_dropdowns(client: AsyncClient) -> None:
    r = await client.get("/assets/new")
    assert r.status_code == 200
    body = r.text
    assert "New fixed asset" in body
    # Depreciation models from the seed are listed
    assert "asset_5_year_linear" in body
    assert "asset_no_depreciation" in body
    # Default dep-expense pre-selects 6-1500
    assert "6-1500" in body


async def test_create_detail_edit_roundtrip(client: AsyncClient) -> None:
    asset_id = await _create_asset_via_http(
        client, name="Smoke laptop", cost="2400.00"
    )

    # Detail page
    detail = await client.get(f"/assets/{asset_id}")
    assert detail.status_code == 200
    assert "Smoke laptop" in detail.text
    assert "2400.00" in detail.text
    assert "Net book value" in detail.text

    # Edit page prefills
    edit = await client.get(f"/assets/{asset_id}/edit")
    assert edit.status_code == 200
    assert "Smoke laptop" in edit.text
    assert 'value="2400.00"' in edit.text


async def test_depreciate_then_archive(client: AsyncClient) -> None:
    asset_id = await _create_asset_via_http(
        client, name="Smoke depreciate", cost="6000.00"
    )
    # Post depreciation through a post-period-lock date
    resp = await client.post(
        f"/assets/{asset_id}/depreciate",
        data={"through_date": "2026-05-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/assets/{asset_id}")

    # Detail now shows last-posted-through
    detail = await client.get(f"/assets/{asset_id}")
    assert "2026-05-01" in detail.text

    # Archive
    arch = await client.post(
        f"/assets/{asset_id}/archive", follow_redirects=False
    )
    assert arch.status_code == 303
    assert arch.headers["location"] == "/assets"

    # Not in active list
    active = await client.get("/assets?status=active")
    assert asset_id not in active.text


async def test_dispose_flow(client: AsyncClient) -> None:
    asset_id = await _create_asset_via_http(
        client,
        name="Smoke dispose",
        cost="1000.00",
        model="asset_no_depreciation",  # no dep → NBV == cost for deterministic math
    )

    # Dispose form renders
    form = await client.get(f"/assets/{asset_id}/dispose")
    assert form.status_code == 200
    assert "Dispose" in form.text

    # Need a cash account ID; reuse the same heuristic.
    body = form.text
    idx = body.index('name="cash_account_id"')
    start = body.index('<option value="', idx) + len('<option value="')
    end = body.index('"', start)
    cash_id = body[start:end]

    post = await client.post(
        f"/assets/{asset_id}/dispose",
        data={
            "disposal_date": "2026-06-01",
            "proceeds": "800.00",  # loss of 200
            "cash_account_id": cash_id,
        },
        follow_redirects=False,
    )
    assert post.status_code == 303

    # Now appears under disposed filter
    disposed = await client.get("/assets?status=disposed")
    assert asset_id in disposed.text

    # Detail shows disposal section
    detail = await client.get(f"/assets/{asset_id}")
    assert "Disposal" in detail.text
    assert "800.00" in detail.text
