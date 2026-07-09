"""Return-shape reconstruction for the pre-accounting module facades (#32 step 4).

When ``settings.preaccounting_base_url`` is set, the public functions in
``services.{quotes,purchase_orders,time_entries}`` route their work here. Each
wrapper POSTs to the module (via ``preaccounting_client``), maps the module's
error responses back to the SAME exception types the engine routers already
catch, and reconstructs the success body into the SAME return shape the
in-process code produced:

* a single record  → the ``Out`` pydantic instance (routers re-validate it via
  ``Out.model_validate(..., from_attributes=True)`` so the wire bytes are
  identical to the in-process path);
* a list           → ``(list[Out], total)``;
* a conversion     → ``(Out, <fact ref>)`` / ``ConvertResult``.

Exceptions are imported lazily to avoid an import cycle (the service modules
import THIS module at load time).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from saebooks.services import preaccounting_client as _client


# --------------------------------------------------------------------------- #
# Error mapping                                                                #
# --------------------------------------------------------------------------- #
def _raise_quote_conflict(body: dict) -> None:
    from saebooks.api.v1.schemas import QuoteOut
    from saebooks.services.quotes import VersionConflict

    raise VersionConflict(QuoteOut.model_validate(body["current"]))


def _raise_po_conflict(body: dict) -> None:
    from saebooks.api.v1.schemas import PurchaseOrderOut
    from saebooks.services.purchase_orders import VersionConflict

    raise VersionConflict(PurchaseOrderOut.model_validate(body["current"]))


def _raise_quote_domain(body: dict) -> None:
    from saebooks.services.quotes import QuoteError

    raise QuoteError(body.get("message", "pre-accounting module rejected the request"))


def _raise_po_domain(body: dict) -> None:
    from saebooks.services.purchase_orders import PurchaseOrderError

    raise PurchaseOrderError(
        body.get("message", "pre-accounting module rejected the request")
    )


def _raise_time_domain(body: dict) -> None:
    from saebooks.services.time_entries import TimeEntryError

    raise TimeEntryError(
        body.get("message", "pre-accounting module rejected the request"),
        code=body.get("code", "time_entry_error"),
    )


def _check(resp, path, *, conflict=None, domain=None):
    """Map non-2xx module responses to the caller's exception types.

    Returns the parsed JSON body on 2xx.
    """
    code = resp.status_code
    if code // 100 == 2:
        return _client.json_body(resp, path)
    body = _client.json_body(resp, path)
    if code == 409 and conflict is not None:
        conflict(body)
    if code == 422 and domain is not None:
        domain(body)
    raise _client.PreAccountingServiceError(
        f"pre-accounting module {path} returned HTTP {code}: {resp.text[:300]}"
    )


# --------------------------------------------------------------------------- #
# Quotes                                                                        #
# --------------------------------------------------------------------------- #
def _quote_out(body: dict):
    from saebooks.api.v1.schemas import QuoteOut

    return QuoteOut.model_validate(body)


async def quote_get(quote_id, tenant_id, company_id):
    resp = await _client.post(
        "quotes/get", {"quote_id": quote_id}, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "quotes/get")
    return _quote_out(body) if body is not None else None


async def quote_list(company_id, tenant_id, **filters):
    resp = await _client.post(
        "quotes/list", filters, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "quotes/list")
    return [_quote_out(x) for x in body["items"]], body["total"]


async def quote_create(company_id, tenant_id, actor, **fields):
    resp = await _client.post(
        "quotes/create",
        {"actor": actor, **fields},
        tenant_id=tenant_id,
        company_id=company_id,
    )
    body = _check(resp, "quotes/create", domain=_raise_quote_domain)
    return _quote_out(body)


async def quote_update(quote_id, actor, expected_version, force, tenant_id, **fields):
    payload = {
        "quote_id": quote_id,
        "actor": actor,
        "expected_version": expected_version,
        "force": force,
        **fields,
    }
    resp = await _client.post("quotes/update", payload, tenant_id=tenant_id)
    body = _check(
        resp, "quotes/update", conflict=_raise_quote_conflict, domain=_raise_quote_domain
    )
    return _quote_out(body)


async def quote_transition(name, quote_id, actor, expected_version, tenant_id):
    payload = {"quote_id": quote_id, "actor": actor, "expected_version": expected_version}
    resp = await _client.post(f"quotes/{name}", payload, tenant_id=tenant_id)
    body = _check(
        resp, f"quotes/{name}", conflict=_raise_quote_conflict, domain=_raise_quote_domain
    )
    return _quote_out(body)


async def quote_convert_to_invoice(quote_id, actor, expected_version, tenant_id):
    payload = {"quote_id": quote_id, "actor": actor, "expected_version": expected_version}
    resp = await _client.post("quotes/convert-to-invoice", payload, tenant_id=tenant_id)
    body = _check(
        resp,
        "quotes/convert-to-invoice",
        conflict=_raise_quote_conflict,
        domain=_raise_quote_domain,
    )
    return _quote_out(body["quote"]), SimpleNamespace(id=uuid.UUID(body["invoice_id"]))


# --------------------------------------------------------------------------- #
# Purchase orders                                                               #
# --------------------------------------------------------------------------- #
def _po_out(body: dict):
    from saebooks.api.v1.schemas import PurchaseOrderOut

    return PurchaseOrderOut.model_validate(body)


async def po_get(po_id, tenant_id, company_id):
    resp = await _client.post(
        "purchase-orders/get", {"po_id": po_id}, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "purchase-orders/get")
    return _po_out(body) if body is not None else None


async def po_list(company_id, tenant_id, **filters):
    resp = await _client.post(
        "purchase-orders/list", filters, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "purchase-orders/list")
    return [_po_out(x) for x in body["items"]], body["total"]


async def po_create(company_id, tenant_id, actor, **fields):
    resp = await _client.post(
        "purchase-orders/create",
        {"actor": actor, **fields},
        tenant_id=tenant_id,
        company_id=company_id,
    )
    body = _check(resp, "purchase-orders/create", domain=_raise_po_domain)
    return _po_out(body)


async def po_update(po_id, actor, expected_version, force, tenant_id, **fields):
    payload = {
        "po_id": po_id,
        "actor": actor,
        "expected_version": expected_version,
        "force": force,
        **fields,
    }
    resp = await _client.post("purchase-orders/update", payload, tenant_id=tenant_id)
    body = _check(
        resp,
        "purchase-orders/update",
        conflict=_raise_po_conflict,
        domain=_raise_po_domain,
    )
    return _po_out(body)


async def po_transition(name, po_id, actor, expected_version, tenant_id):
    payload = {"po_id": po_id, "actor": actor, "expected_version": expected_version}
    resp = await _client.post(f"purchase-orders/{name}", payload, tenant_id=tenant_id)
    body = _check(
        resp,
        f"purchase-orders/{name}",
        conflict=_raise_po_conflict,
        domain=_raise_po_domain,
    )
    return _po_out(body)


async def po_convert_to_bill(po_id, actor, expected_version, tenant_id, **fields):
    payload = {
        "po_id": po_id,
        "actor": actor,
        "expected_version": expected_version,
        **fields,
    }
    resp = await _client.post("purchase-orders/convert-to-bill", payload, tenant_id=tenant_id)
    body = _check(
        resp,
        "purchase-orders/convert-to-bill",
        conflict=_raise_po_conflict,
        domain=_raise_po_domain,
    )
    bill = SimpleNamespace(
        id=uuid.UUID(body["bill_id"]), number=body.get("bill_number")
    )
    return _po_out(body["purchase_order"]), bill


# --------------------------------------------------------------------------- #
# Time entries                                                                  #
# --------------------------------------------------------------------------- #
def _te_out(body: dict):
    from saebooks.api.v1.schemas import TimeEntryOut

    return TimeEntryOut.model_validate(body)


async def te_get(company_id, entry_id, tenant_id):
    resp = await _client.post(
        "time-entries/get", {"entry_id": entry_id}, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "time-entries/get")
    return _te_out(body) if body is not None else None


async def te_list(company_id, tenant_id, filters, limit, offset):
    payload: dict[str, Any] = {"limit": limit, "offset": offset}
    if filters is not None:
        payload.update(
            {
                "user_id": filters.user_id,
                "contact_id": filters.contact_id,
                "project_id": filters.project_id,
                "approval_status": filters.approval_status,
                "billable_only": filters.billable_only,
                "uninvoiced_only": filters.uninvoiced_only,
                "date_from": filters.date_from,
                "date_to": filters.date_to,
            }
        )
    resp = await _client.post(
        "time-entries/list", payload, tenant_id=tenant_id, company_id=company_id
    )
    body = _client.ensure_ok(resp, "time-entries/list")
    return [_te_out(x) for x in body["items"]], body["total"]


async def te_list_week(company_id, user_id, week_start, tenant_id):
    resp = await _client.post(
        "time-entries/list-week",
        {"user_id": user_id, "week_start": week_start},
        tenant_id=tenant_id,
        company_id=company_id,
    )
    body = _client.ensure_ok(resp, "time-entries/list-week")
    return [_te_out(x) for x in body["items"]]


async def te_create(company_id, tenant_id, **fields):
    resp = await _client.post(
        "time-entries/create", fields, tenant_id=tenant_id, company_id=company_id
    )
    body = _check(resp, "time-entries/create", domain=_raise_time_domain)
    return _te_out(body)


async def te_update(company_id, entry_id, tenant_id, expected_version, force, fields):
    payload = {
        "entry_id": entry_id,
        "expected_version": expected_version,
        "force": force,
        "fields": fields,
    }
    resp = await _client.post(
        "time-entries/update", payload, tenant_id=tenant_id, company_id=company_id
    )
    body = _check(resp, "time-entries/update", domain=_raise_time_domain)
    return _te_out(body)


async def te_mutate(name, company_id, entry_id, tenant_id, **extra):
    resp = await _client.post(
        f"time-entries/{name}",
        {"entry_id": entry_id, **extra},
        tenant_id=tenant_id,
        company_id=company_id,
    )
    body = _check(resp, f"time-entries/{name}", domain=_raise_time_domain)
    return _te_out(body)


async def te_convert_to_invoice_line(company_id, tenant_id, entry_ids, invoice_id, contact_id):
    from decimal import Decimal

    from saebooks.services.time_entries import ConvertResult

    payload = {
        "entry_ids": entry_ids,
        "invoice_id": invoice_id,
        "contact_id": contact_id,
    }
    resp = await _client.post(
        "time-entries/convert-to-invoice-line",
        payload,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    body = _check(
        resp, "time-entries/convert-to-invoice-line", domain=_raise_time_domain
    )
    return ConvertResult(
        invoice_id=uuid.UUID(body["invoice_id"]),
        invoice_line_id=uuid.UUID(body["invoice_line_id"]),
        converted_entry_ids=[uuid.UUID(e) for e in body["converted_entry_ids"]],
        total_hours=Decimal(body["total_hours"]),
        total_amount=Decimal(body["total_amount"]),
    )
