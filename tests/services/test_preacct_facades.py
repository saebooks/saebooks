"""Facade tests for the pre-accounting env-flag delegation (#32 step 4).

Flag OFF (``PREACCOUNTING_BASE_URL`` empty) → ``delegating()`` is False and the
in-process service code runs unchanged (the existing ``tests/api/v1/test_quotes``
suite proves the behaviour is identical; here we just assert the flag gate).

Flag ON → the public service functions POST to the module. These tests
respx-mock the module and assert three things per call:
  1. the request reaches the right module path,
  2. it carries the tenant context (``X-Tenant-Id``) and the service token
     (``X-PreAccounting-Token``),
  3. the JSON response is reconstructed back into the SAME return shape the
     in-process path produced (a ``QuoteOut`` instance / ``(QuoteOut, ref)`` /
     a raised ``VersionConflict``).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

import httpx
import pytest
import respx

from saebooks.api.v1.schemas import QuoteOut
from saebooks.services import preaccounting_client as pac
from saebooks.services import quotes as quotes_svc

_BASE = "http://preacct-module:8080"
_SVC_TOKEN = "svc-token-xyz"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_COMPANY = uuid.uuid4()


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch by STRING path: the licence-resolver fixtures elsewhere in the
    # suite REPLACE saebooks.config.settings wholesale, so an object reference
    # captured at this module's import time can go stale in full-suite order
    # (the client resolves settings at call time and would read the new
    # object while we patched the old one — found by the 2026-07-04 soak run).
    monkeypatch.setattr("saebooks.config.settings.preaccounting_base_url", _BASE)
    monkeypatch.setattr("saebooks.config.settings.preaccounting_service_token", _SVC_TOKEN)


def _quote_out_json(**overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "company_id": str(_COMPANY),
        "tenant_id": str(_TENANT),
        "customer_id": str(uuid.uuid4()),
        "number": None,
        "issue_date": "2026-05-01",
        "expiry_date": "2026-05-29",
        "status": "DRAFT",
        "subtotal": "1500.00",
        "tax_total": "150.00",
        "total": "1650.00",
        "currency": "AUD",
        "validity_days": 28,
        "deposit_pct": "50",
        "late_fee_pct_per_month": "2.5",
        "is_supply_only": False,
        "title": None,
        "scope": None,
        "notes": "delegated",
        "terms": None,
        "accepted_at": None,
        "declined_at": None,
        "invoiced_at": None,
        "invoice_id": None,
        "version": 1,
        "created_at": datetime(2026, 5, 1, 0, 0, 0).isoformat(),
        "updated_at": datetime(2026, 5, 1, 0, 0, 0).isoformat(),
        "lines": [],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Flag gate                                                                     #
# --------------------------------------------------------------------------- #
def test_delegating_reflects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.preaccounting_base_url", "")
    assert pac.delegating() is False
    monkeypatch.setattr("saebooks.config.settings.preaccounting_base_url", _BASE)
    assert pac.delegating() is True


# --------------------------------------------------------------------------- #
# Delegation happy paths                                                        #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_quote_create_delegates_and_maps_back(flag_on: None) -> None:
    out = _quote_out_json()
    route = respx.post(f"{_BASE}/module/preaccounting/quotes/create").mock(
        return_value=httpx.Response(201, json=out)
    )

    result = await quotes_svc.api_create(
        object(),  # session unused on the delegate path
        _COMPANY,
        _TENANT,
        actor="api:test",
        customer_id=uuid.UUID(out["customer_id"]),
        issue_date=date(2026, 5, 1),
        lines=[{"description": "Design", "quantity": "10", "unit_price": "150.00"}],
        notes="delegated",
    )

    # return shape maps back to a QuoteOut with identical field values
    assert isinstance(result, QuoteOut)
    assert str(result.id) == out["id"]
    assert result.notes == "delegated"
    assert result.total == out["total"] or str(result.total) == out["total"]

    # tenant context + service token travelled on the wire
    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == str(_TENANT)
    assert req.headers["X-Company-Id"] == str(_COMPANY)
    assert req.headers["X-PreAccounting-Token"] == _SVC_TOKEN


@respx.mock
async def test_quote_get_null_maps_to_none(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/preaccounting/quotes/get").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    result = await quotes_svc.api_get(object(), uuid.uuid4(), tenant_id=_TENANT)
    assert result is None


@respx.mock
async def test_quote_convert_maps_tuple(flag_on: None) -> None:
    invoice_id = str(uuid.uuid4())
    out = _quote_out_json(status="INVOICED", invoice_id=invoice_id, version=4)
    respx.post(f"{_BASE}/module/preaccounting/quotes/convert-to-invoice").mock(
        return_value=httpx.Response(200, json={"quote": out, "invoice_id": invoice_id})
    )
    quote, inv = await quotes_svc.convert_to_invoice(
        object(), uuid.UUID(out["id"]), actor="api:test", expected_version=3, tenant_id=_TENANT
    )
    assert isinstance(quote, QuoteOut)
    assert quote.status == "INVOICED"
    assert str(inv.id) == invoice_id  # router reads ``inv.id``


# --------------------------------------------------------------------------- #
# Error mapping                                                                 #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_quote_update_conflict_reraised(flag_on: None) -> None:
    current = _quote_out_json(version=7)
    respx.post(f"{_BASE}/module/preaccounting/quotes/update").mock(
        return_value=httpx.Response(
            409, json={"detail": "version mismatch", "current": current}
        )
    )
    with pytest.raises(quotes_svc.VersionConflict) as ei:
        await quotes_svc.api_update(
            object(),
            uuid.UUID(current["id"]),
            actor="api:test",
            expected_version=1,
            tenant_id=_TENANT,
        )
    # the engine router reads exc.current via QuoteOut.model_validate
    assert QuoteOut.model_validate(ei.value.current).version == 7


@respx.mock
async def test_quote_domain_error_reraised(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/preaccounting/quotes/send").mock(
        return_value=httpx.Response(
            422, json={"code": "quote_error", "message": "expected DRAFT"}
        )
    )
    with pytest.raises(quotes_svc.QuoteError):
        await quotes_svc.api_send(
            object(), uuid.uuid4(), actor="api:test", expected_version=1, tenant_id=_TENANT
        )


@respx.mock
async def test_transport_failure_raises_service_error(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/preaccounting/quotes/get").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(pac.PreAccountingServiceError):
        await quotes_svc.api_get(object(), uuid.uuid4(), tenant_id=_TENANT)
