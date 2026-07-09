from httpx import AsyncClient


async def test_healthz(client: AsyncClient) -> None:
    # The engine is a pure API service (#32): the canonical liveness path
    # is /api/v1/healthz. The legacy top-level /healthz was removed with
    # the embedded HTML router layer.
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["edition"] in {"community", "enterprise"}


async def test_api_license(client: AsyncClient) -> None:
    r = await client.get("/api/v1/license")
    assert r.status_code == 200
    body = r.json()
    assert "edition" in body
    assert body["edition"] in {"community", "offline", "business", "pro", "enterprise"}
    assert "flags" in body
    assert isinstance(body["flags"], dict)
    assert "multi_company" in body["flags"]
    assert "all_flags" in body
    assert "tier_order" in body
    assert "multi_company" in body["all_flags"]
