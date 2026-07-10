"""Facade tests for the capture env-flag delegation (#32 step 5).

Flag OFF (``CAPTURE_BASE_URL`` empty) → ``delegating()`` is False and the
engine runs its in-process code unchanged (the existing imports / bank_feeds /
ai_extraction suites prove the behaviour; here we just assert the flag gate).

Flag ON → the delegation surface POSTs/GETs to the module. These tests
respx-mock the module and assert:
  1. the request reaches the right module path,
  2. it carries the tenant context (``X-Tenant-Id``) and the service token
     (``X-Capture-Token``),
  3. the module's status + JSON body are mirrored back verbatim (route-level
     proxy) or the dict is returned unchanged (service-level ai_extraction).

Per the 2026-07-04 test-hygiene rule, settings are patched by STRING path only.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from saebooks.services import ai_extraction as ai_svc
from saebooks.services import capture_client as cc
from saebooks.services import capture_facades as cf
from saebooks.services.circuit_breaker import CircuitBreaker

_BASE = "http://capture-module:8080"
_SVC_TOKEN = "capture-svc-token"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_COMPANY = uuid.uuid4()


@pytest.fixture(autouse=True)
def _reset_capture_breaker() -> None:
    """Isolate the module-level runtime breaker between tests (M2 wave 2a) —
    it is a singleton, so a failure recorded by one test must not carry over
    and unexpectedly trip the breaker in the next."""
    cc._reset_breaker_for_tests()
    yield
    cc._reset_breaker_for_tests()


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_base_url", _BASE)
    monkeypatch.setattr("saebooks.config.settings.capture_service_token", _SVC_TOKEN)


# --------------------------------------------------------------------------- #
# Flag gate                                                                     #
# --------------------------------------------------------------------------- #
def test_delegating_reflects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_base_url", "")
    assert cc.delegating() is False
    monkeypatch.setattr("saebooks.config.settings.capture_base_url", _BASE)
    assert cc.delegating() is True


# --------------------------------------------------------------------------- #
# ai_extraction — service-level facade                                          #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_extract_document_delegates_and_maps_back(flag_on: None) -> None:
    parsed = {
        "vendor_name": "Beta Co",
        "total": "42.00",
        "line_items": [],
        "extraction_error": None,
    }
    route = respx.post(f"{_BASE}/module/capture/documents/extract").mock(
        return_value=httpx.Response(200, json=parsed)
    )

    # No explicit settings → delegate.
    result = await ai_svc.extract_document(b"\xff\xd8fake", "image/jpeg")
    assert result["vendor_name"] == "Beta Co"
    assert result["total"] == "42.00"

    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Capture-Token"] == _SVC_TOKEN
    # multipart upload carried the file part
    assert b"form-data" in req.content


@respx.mock
async def test_extract_document_503_maps_to_not_configured(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/capture/documents/extract").mock(
        return_value=httpx.Response(503, json={"detail": "LITELLM_API_KEY not set"})
    )
    with pytest.raises(ai_svc.AiExtractionNotConfiguredError):
        await ai_svc.extract_document(b"x", "image/png")


@respx.mock
async def test_extract_document_transport_error(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/capture/documents/extract").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(cc.CaptureServiceError):
        await ai_svc.extract_document(b"x", "image/png")


def test_extract_document_explicit_settings_never_delegates(
    flag_on: None,
) -> None:
    """An explicit ``settings`` override always runs in-process (test path),
    never delegates — even with the flag on. Verified by the absence of any
    respx route: an in-process run with an empty key raises NotConfigured."""

    async def _run() -> None:
        from saebooks.config import Settings

        s = Settings(LITELLM_API_KEY="")
        with pytest.raises(ai_svc.AiExtractionNotConfiguredError):
            await ai_svc.extract_document(b"x", "image/png", settings=s)

    import asyncio

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Route-level proxies (imports + bank-feeds)                                    #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_mirror_post_forwards_and_mirrors_status(flag_on: None) -> None:
    route = respx.post(f"{_BASE}/module/capture/imports/wizards").mock(
        return_value=httpx.Response(201, json={"wizard_id": "w1", "step": 0, "state": {}})
    )
    resp = await cf.mirror_post(
        "imports/wizards",
        b'{"kind":"bank_csv","initial":{}}',
        content_type="application/json",
        tenant_id=_TENANT,
        idempotency_key="idem-1",
    )
    assert resp.status_code == 201
    import json as _json

    assert _json.loads(bytes(resp.body))["wizard_id"] == "w1"

    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == str(_TENANT)
    assert req.headers["X-Capture-Token"] == _SVC_TOKEN
    assert req.headers["X-Idempotency-Key"] == "idem-1"
    assert req.content == b'{"kind":"bank_csv","initial":{}}'


@respx.mock
async def test_mirror_post_passes_through_error_status(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/capture/imports/wizards/w1/commit").mock(
        return_value=httpx.Response(422, json={"code": "period_locked"})
    )
    resp = await cf.mirror_post(
        "imports/wizards/w1/commit",
        b"{}",
        content_type="application/json",
        tenant_id=_TENANT,
        company_id=_COMPANY,
    )
    assert resp.status_code == 422


@respx.mock
async def test_application_error_status_does_not_trip_breaker(flag_on: None) -> None:
    """A non-2xx the module deliberately RETURNS (e.g. 422 period_locked) is
    NOT a transport failure — the module answered. Repeated 422s must not
    trip the breaker (only unreachability should)."""
    respx.post(f"{_BASE}/module/capture/imports/wizards/w1/commit").mock(
        return_value=httpx.Response(422, json={"code": "period_locked"})
    )
    for _ in range(10):  # far more than any reasonable failure_threshold
        resp = await cf.mirror_post(
            "imports/wizards/w1/commit",
            b"{}",
            content_type="application/json",
            tenant_id=_TENANT,
            company_id=_COMPANY,
        )
        assert resp.status_code == 422
    assert cc._breaker.state.value == "closed"
    assert cc.delegating() is True


@respx.mock
async def test_mirror_post_forwards_retry_after_header(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/capture/imports/wizards").mock(
        return_value=httpx.Response(
            503, json={"code": "request_in_flight"}, headers={"Retry-After": "1"}
        )
    )
    resp = await cf.mirror_post(
        "imports/wizards",
        b"{}",
        content_type="application/json",
        tenant_id=_TENANT,
    )
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"


@respx.mock
async def test_mirror_get_forwards_company_and_mirrors(flag_on: None) -> None:
    route = respx.get(f"{_BASE}/module/capture/bank-feeds/connections").mock(
        return_value=httpx.Response(200, json=[{"id": str(uuid.uuid4())}])
    )
    resp = await cf.mirror_get(
        "bank-feeds/connections", tenant_id=_TENANT, company_id=_COMPANY
    )
    assert resp.status_code == 200
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == str(_TENANT)
    assert req.headers["X-Company-Id"] == str(_COMPANY)


@respx.mock
async def test_mirror_delete_forwards_and_mirrors(flag_on: None) -> None:
    respx.delete(f"{_BASE}/module/capture/bank-feeds/connections/c1").mock(
        return_value=httpx.Response(200, json={"id": "c1", "status": "revoked", "stub": True})
    )
    resp = await cf.mirror_delete(
        "bank-feeds/connections/c1", tenant_id=_TENANT
    )
    assert resp.status_code == 200
    import json as _json

    assert _json.loads(bytes(resp.body))["status"] == "revoked"


@respx.mock
async def test_transport_failure_raises_service_error(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/capture/bank-feeds/sync").mock(
        side_effect=httpx.ConnectError("no route")
    )
    with pytest.raises(cc.CaptureServiceError):
        await cf.mirror_post(
            "bank-feeds/sync",
            b"{}",
            content_type="application/json",
            tenant_id=_TENANT,
        )


# --------------------------------------------------------------------------- #
# Runtime circuit breaker (M2 wave 2a, P0a/P0b) — degrade, don't hammer          #
# --------------------------------------------------------------------------- #
class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _small_breaker(monkeypatch: pytest.MonkeyPatch, *, threshold: int, cooldown: float) -> _FakeClock:
    """Swap the module's runtime breaker for one with a low threshold and an
    injected clock, so trip/cooldown/half-open can be exercised without
    5 real failures or real sleeps."""
    clock = _FakeClock()
    breaker = CircuitBreaker(
        "capture-test", failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock
    )
    monkeypatch.setattr(cc, "_breaker", breaker)
    return clock


@respx.mock
async def test_breaker_trips_open_and_falls_back_without_hammering(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After N consecutive transport failures the breaker opens; the
    engine's existing capture-flag call sites fall back to in-process
    automatically (``delegating()`` -> False) with NO further network
    attempt — the "don't hammer a down service" requirement."""
    _small_breaker(monkeypatch, threshold=2, cooldown=30.0)
    route = respx.post(f"{_BASE}/module/capture/bank-feeds/sync").mock(
        side_effect=httpx.ConnectError("no route")
    )

    for _ in range(2):
        with pytest.raises(cc.CaptureServiceError):
            await cf.mirror_post(
                "bank-feeds/sync", b"{}", content_type="application/json", tenant_id=_TENANT
            )

    assert route.call_count == 2
    assert cc._breaker.state.value == "open"
    # delegating() now returns False without attempting the network — the
    # ~15 in-process-capable call sites (bank_feeds.py, imports.py, ...)
    # fall back to their existing in-process branch automatically.
    assert cc.delegating() is False
    assert route.call_count == 2, "breaker-open must not attempt another request"


@respx.mock
async def test_breaker_half_open_probe_success_closes_and_resumes_delegation(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _small_breaker(monkeypatch, threshold=1, cooldown=10.0)
    route = respx.post(f"{_BASE}/module/capture/bank-feeds/sync")
    route.side_effect = httpx.ConnectError("no route")

    with pytest.raises(cc.CaptureServiceError):
        await cf.mirror_post(
            "bank-feeds/sync", b"{}", content_type="application/json", tenant_id=_TENANT
        )
    assert cc.delegating() is False  # OPEN, within cooldown

    clock.advance(10.0)
    assert cc.delegating() is True  # cooldown elapsed -> half-open probe granted

    route.side_effect = None
    route.mock(return_value=httpx.Response(200, json={"synced": True}))
    resp = await cf.mirror_post(
        "bank-feeds/sync", b"{}", content_type="application/json", tenant_id=_TENANT
    )
    assert resp.status_code == 200
    assert cc._breaker.state.value == "closed"
    assert cc.delegating() is True
