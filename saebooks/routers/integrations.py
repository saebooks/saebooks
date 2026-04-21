"""Integrations router — LEI lookup, Stripe webhook, ATO prefill, Paperless.

Three separate surfaces:

* ``POST /contacts/lei-lookup`` + ``POST /contacts/{id}/lei-apply`` —
  mirror of the ABR routes in ``contacts.py``, gated on
  ``FLAG_LEI_LOOKUP``. Lives here (not in contacts.py) because LEI is
  a Phase-5 integration and we don't want to churn the contacts
  module. Paths are under ``/contacts/...`` so they share the
  contact-form UX.
* ``POST /webhooks/stripe`` — public Stripe webhook endpoint. No auth
  gate beyond the HMAC signature verifier (auth via Authentik would
  break the webhook since Stripe can't carry forward-auth cookies).
* ``GET /admin/integrations/`` — landing page linking to each flow +
  status badges.
* ``POST /admin/integrations/ato-prefill`` — stub, returns 501 with a
  human-readable JSON explaining the Batch KK dependency.
* ``POST /admin/integrations/paperless/attach`` — link a Paperless
  doc-id to a journal entry's ``attachments`` JSONB.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import contacts as contacts_svc
from saebooks.services.features import (
    FLAG_LEI_LOOKUP,
    is_enabled,
    require_feature,
)
from saebooks.services.integrations import (
    LeiError,
    LeiNotFoundError,
    PaperlessClient,
    PaperlessError,
    PaperlessNotConfiguredError,
    StripeError,
    StripeSignatureError,
    apply_to_contact,
    attach_to_journal,
    handle_payment_intent_succeeded,
    lookup_lei,
    verify_signature,
)
from saebooks.services.integrations.stripe import parse_event
from saebooks.web import templates

logger = logging.getLogger("saebooks.integrations")
router = APIRouter()


# ---------------------------------------------------------------------------
# LEI lookup (Enterprise — FLAG_LEI_LOOKUP).
# Registered under /contacts/ so Jinja partials can be rendered into the
# existing contact form with an HTMX hx-post. Routes declared ABOVE any
# potential /{contact_id} matcher in contacts.py — but this router is
# included AFTER contacts.py in main.py, so we mount at /contacts/lei-lookup
# specifically (not /contacts, which would clash with the list page).
# ---------------------------------------------------------------------------


@router.post(
    "/contacts/lei-lookup",
    response_class=HTMLResponse,
    dependencies=[Depends(require_feature(FLAG_LEI_LOOKUP))],
)
async def contacts_lei_lookup(
    request: Request,
    lei: str = Form(...),
) -> HTMLResponse:
    """HTMX: look up an LEI and render a preview fragment."""
    try:
        result = await lookup_lei(lei, settings=settings)
    except LeiNotFoundError as exc:
        return templates.TemplateResponse(
            request,
            "integrations/_lei_error.html",
            {"message": str(exc)},
            status_code=404,
        )
    except LeiError as exc:
        return templates.TemplateResponse(
            request,
            "integrations/_lei_error.html",
            {"message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "integrations/_lei_result.html",
        {"result": result},
    )


@router.post(
    "/contacts/{contact_id}/lei-apply",
    response_class=HTMLResponse,
    dependencies=[Depends(require_feature(FLAG_LEI_LOOKUP))],
)
async def contacts_lei_apply(
    request: Request,
    contact_id: UUID,
    lei: str = Form(...),
    overwrite: str = Form(""),
) -> HTMLResponse:
    """Fetch GLEIF and merge into the live Contact row."""
    try:
        result = await lookup_lei(lei, settings=settings)
    except LeiNotFoundError as exc:
        return templates.TemplateResponse(
            request,
            "integrations/_lei_error.html",
            {"message": str(exc)},
            status_code=404,
        )
    except LeiError as exc:
        return templates.TemplateResponse(
            request,
            "integrations/_lei_error.html",
            {"message": str(exc)},
            status_code=400,
        )

    async with AsyncSessionLocal() as session:
        contact = await contacts_svc.get(session, contact_id)
        if contact is None:
            raise HTTPException(404, "Contact not found")
        changed = apply_to_contact(
            contact, result, overwrite=overwrite.lower() in {"1", "true", "on"}
        )
        await session.commit()

    return templates.TemplateResponse(
        request,
        "integrations/_lei_applied.html",
        {
            "result": result,
            "changed": changed,
            "contact_id": contact_id,
        },
    )


# ---------------------------------------------------------------------------
# Stripe webhook (public — auth is HMAC signature verification).
# ---------------------------------------------------------------------------


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Stripe webhook receiver. Verifies signature, processes events.

    Always returns 2xx on valid signatures — including for events we
    choose to ignore — so Stripe doesn't retry. Non-2xx is reserved
    for signature failures + config errors that warrant retry.
    """
    if not settings.stripe_webhook_secret:
        # 503 so Stripe retries once the admin configures the secret.
        return JSONResponse(
            {"error": "Stripe webhook not configured"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not sig_header:
        return JSONResponse(
            {"error": "Missing Stripe-Signature header"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        verify_signature(raw, sig_header, settings.stripe_webhook_secret)
    except StripeSignatureError as exc:
        # 400 — Stripe won't retry on 4xx (except 409). This means a
        # real signature failure is loud on the Stripe dashboard.
        logger.warning("stripe: signature verification failed: %s", exc)
        return JSONResponse(
            {"error": "Signature verification failed"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        event = parse_event(raw)
    except StripeError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST
        )

    etype = event.get("type", "")
    logger.info("stripe: received event id=%s type=%s", event.get("id"), etype)

    handled: bool = False
    if etype == "payment_intent.succeeded":
        async with AsyncSessionLocal() as session:
            pay = await handle_payment_intent_succeeded(
                session, event, settings=settings
            )
            if pay is not None:
                await session.commit()
                handled = True

    return JSONResponse({"received": True, "handled": handled})


# ---------------------------------------------------------------------------
# Admin → Integrations landing + Paperless + ATO Prefill.
# ---------------------------------------------------------------------------


@router.get("/admin/integrations", response_class=HTMLResponse)
@router.get("/admin/integrations/", response_class=HTMLResponse)
async def integrations_index(request: Request) -> HTMLResponse:
    """Landing page with a status matrix for each integration."""
    statuses = {
        "paperless": bool(
            settings.paperless_api_token
            and (settings.paperless_api_url or settings.paperless_url)
        ),
        "lei": is_enabled(FLAG_LEI_LOOKUP),
        "stripe": bool(settings.stripe_webhook_secret),
        "ato_prefill": False,  # Batch KK — not wired
    }
    return templates.TemplateResponse(
        request,
        "integrations/index.html",
        {
            "edition": settings.edition,
            "statuses": statuses,
            "lei_enabled": is_enabled(FLAG_LEI_LOOKUP),
        },
    )


@router.post("/admin/integrations/paperless/attach")
async def paperless_attach(
    request: Request,
    journal_id: UUID = Form(...),  # noqa: B008
    document_id: int = Form(...),
) -> JSONResponse:
    """Attach a Paperless document to a journal entry's attachments JSONB."""
    try:
        async with PaperlessClient(settings=settings) as pc:
            attachment = await pc.fetch_attachment(document_id)
    except PaperlessNotConfiguredError:
        return JSONResponse(
            {"error": "Paperless is not configured"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except PaperlessError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST
        )

    async with AsyncSessionLocal() as session:
        try:
            entry = await attach_to_journal(session, journal_id, attachment)
        except PaperlessError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        await session.commit()
    return JSONResponse(
        {
            "journal_id": str(entry.id),
            "document_id": attachment.document_id,
            "title": attachment.title,
            "url": attachment.browser_url,
        }
    )


@router.post("/admin/integrations/ato-prefill")
async def ato_prefill_stub() -> JSONResponse:
    """Stub — returns 501 until Batch KK lands."""
    return JSONResponse(
        {
            "error": "Not implemented",
            "detail": (
                "ATO BAS prefill needs an AUSkey / M2M certificate "
                "and SBR registration (Batch KK). Populate BAS "
                "worksheets manually via /reports/bas for now."
            ),
        },
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
    )


# ---------------------------------------------------------------------------
# Lightweight health echo — used by tests + the status matrix.
# ---------------------------------------------------------------------------


@router.get("/admin/integrations/healthz", response_class=PlainTextResponse)
async def integrations_healthz() -> PlainTextResponse:
    """Plain-text dump of which integrations are configured.

    Machine-readable one-line summary for uptime monitoring.
    """
    flags: dict[str, Any] = {
        "paperless": bool(settings.paperless_api_token),
        "lei": is_enabled(FLAG_LEI_LOOKUP),
        "stripe": bool(settings.stripe_webhook_secret),
        "ato_prefill": False,
    }
    return PlainTextResponse(
        " ".join(f"{k}={str(v).lower()}" for k, v in flags.items()),
        media_type="text/plain",
    )


# Expose a few symbols for test injection.
__all__ = ["router"]
