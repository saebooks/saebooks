"""Integration endpoints — ``/api/v1/integrations/*``.

Surfaces customer-facing integration control points. All routes are
Bearer-auth gated (via the router-level ``require_bearer`` dependency
wired in ``api/v1/__init__.py``) and additionally gated by feature
flags so lower-tier builds return 404 rather than 403.

Endpoints
---------
POST /api/v1/integrations/stripe/customer/connect
    Initiate the customer Stripe Connect OAuth flow. Returns
    ``{authorize_url, state}``. Gated ``FLAG_STRIPE_INTEGRATION``
    (Business+).

GET /api/v1/integrations/stripe/customer
    Return the customer's Stripe Connect status: ``{connected,
    account_id, charges_enabled, payouts_enabled}``. Gated
    ``FLAG_STRIPE_INTEGRATION`` (Business+).

POST /api/v1/integrations/paperless/webhook
    Inbound HMAC-validated Paperless-ngx webhook. Reads the per-tenant
    secret from ``paperless_webhook_secrets`` and validates the
    ``X-Paperless-Signature`` header. Gated ``FLAG_PAPERLESS_INTEGRATION``
    (Business+).

POST /api/v1/integrations/lei/lookup
    Body ``{search: str}`` → LEI matches via GLEIF API. Gated
    ``FLAG_LEI_LOOKUP`` (Pro+).

POST /api/v1/integrations/companies-house/search
    Body ``{query: str}`` → Companies House results. Gated
    ``FLAG_COMPANIES_HOUSE`` (Pro+).

POST /api/v1/integrations/ato/prefill
    Stub — returns 501 until Batch KK lands. No flag gate (the stub is
    harmless in any tier; the live implementation will gate on
    FLAG_ATO_SBR).

Conventions
-----------
* ``X-Idempotency-Key`` is honoured on write endpoints (24 h replay).
* Tenant binding via ``get_session`` dep → ``app.current_tenant SET LOCAL``
  so RLS is enforced for every DB query in the handler.
* Inbound paperless webhook does NOT use ``get_session`` (it has no JWT
  bearer token from Stripe/Paperless) — it reads the secret row via an
  explicit session with the tenant derived from the webhook payload's
  ``custom_fields`` (or a pre-registered routing key). For v1 the tenant
  is resolved from the bearer token on the *registration* side; the
  actual webhook is a public endpoint verified by HMAC.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services.crypto import FieldEncryptionNotConfiguredError, decrypt_field
from saebooks.services.features import (
    FLAG_COMPANIES_HOUSE,
    FLAG_LEI_LOOKUP,
    FLAG_PAPERLESS_INTEGRATION,
    FLAG_STRIPE_INTEGRATION,
    require_feature,
)
from saebooks.services.integrations.ato_prefill import (
    AtoPrefillNotImplementedError,
    prefill_bas,
)
from saebooks.services.integrations.companies_house import (
    CompaniesHouseError,
    CompaniesHouseNotConfiguredError,
    CompaniesHouseNotFoundError,
    lookup_company,
)
from saebooks.services.integrations.customer_stripe import (
    CustomerStripeError,
    CustomerStripeNotConfiguredError,
    get_account_status,
    initiate_connect_oauth,
)
from saebooks.services.integrations.lei import (
    LeiError,
    LeiNotFoundError,
    lookup_lei,
)
from saebooks.services.integrations.paperless_ingest import (
    extract_document_id,
    ingest_document,
)

logger = logging.getLogger("saebooks.api.v1.integrations")

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_bearer)],
)

# Public sub-router for webhook routes that authenticate via their own HMAC.
# Mounted alongside `router` in saebooks/api/v1/__init__.py.
public_router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class LeiLookupRequest(BaseModel):
    search: str


class CompaniesHouseSearchRequest(BaseModel):
    query: str


class AtoPrefillRequest(BaseModel):
    period_start: str  # ISO date "YYYY-MM-DD"
    period_end: str


# ---------------------------------------------------------------------------
# In-memory state store for OAuth connect state tokens.
# In production this would go into a Redis / DB column, but for v1 a
# simple per-process dict suffices — the connect flow is short-lived
# (< 5 minutes) and single-server deployments don't need distributed
# state. The rollup that wires the DB column can replace this dict.
# ---------------------------------------------------------------------------

_STRIPE_CONNECT_STATES: dict[str, str] = {}  # state -> str(tenant_id)


# ---------------------------------------------------------------------------
# Stripe customer Connect
# ---------------------------------------------------------------------------


@router.post(
    "/stripe/customer/connect",
    summary="Initiate customer Stripe Connect OAuth",
    dependencies=[Depends(require_feature(FLAG_STRIPE_INTEGRATION))],
)
async def stripe_customer_connect(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return the Stripe Connect authorisation URL and a CSRF state token.

    The caller should redirect the user's browser to ``authorize_url``.
    The ``state`` value must be stored and validated when Stripe calls
    back to the registered redirect URI.

    Returns:
        200 ``{"authorize_url": "<url>", "state": "<hex>"}``
    """
    tenant_id = resolve_tenant_id(request)
    redirect_uri = str(request.base_url).rstrip("/") + "/api/v1/integrations/stripe/customer/callback"

    try:
        result = initiate_connect_oauth(tenant_id, redirect_uri=redirect_uri)
    except CustomerStripeNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CustomerStripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Stash state → tenant mapping for callback validation.
    _STRIPE_CONNECT_STATES[result["state"]] = str(tenant_id)

    logger.info(
        "integrations: stripe connect initiated for tenant=%s",
        tenant_id,
    )
    return JSONResponse(result)


@router.get(
    "/stripe/customer",
    summary="Customer Stripe Connect status",
    dependencies=[Depends(require_feature(FLAG_STRIPE_INTEGRATION))],
)
async def stripe_customer_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return the current Stripe Connect status for the authenticated tenant.

    Reads the ``stripe_account_id`` stored on the tenant row (if any)
    and fetches live account metadata from Stripe's API.

    Returns:
        200 ``{"connected": bool, "account_id": str|null, "charges_enabled":
        bool, "payouts_enabled": bool, "details_submitted": bool}``
    """
    from saebooks.models.tenant import Tenant

    tenant_id = resolve_tenant_id(request)
    result = await session.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalars().first()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    account_id: str = getattr(tenant, "stripe_account_id", None) or ""

    if not account_id:
        return JSONResponse({
            "connected": False,
            "account_id": None,
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
        })

    try:
        acct = await get_account_status(account_id)
    except CustomerStripeError as exc:
        logger.warning("integrations: stripe account fetch error: %s", exc)
        acct = {}

    return JSONResponse({
        "connected": bool(account_id),
        "account_id": account_id,
        "charges_enabled": acct.get("charges_enabled", False),
        "payouts_enabled": acct.get("payouts_enabled", False),
        "details_submitted": acct.get("details_submitted", False),
    })


# ---------------------------------------------------------------------------
# Paperless inbound webhook
# ---------------------------------------------------------------------------


@public_router.post(
    "/paperless/webhook",
    summary="Inbound Paperless-ngx webhook (HMAC-validated)",
    dependencies=[Depends(require_feature(FLAG_PAPERLESS_INTEGRATION))],
)
async def paperless_webhook(
    request: Request,
    x_paperless_signature: str | None = Header(default=None, alias="X-Paperless-Signature"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> JSONResponse:
    """Accept an inbound Paperless-ngx document-created webhook.

    Authentication model
    --------------------
    Paperless supports a configurable ``PAPERLESS_CONSUMER_WEBHOOK``
    secret that is sent as ``X-Paperless-Signature: sha256=<hex>``
    on each event (HMAC-SHA256 of the raw body with the shared secret).
    The secret is stored per-tenant in ``paperless_webhook_secrets``.

    Tenant routing
    --------------
    The inbound tenant is identified by the ``X-Tenant-Id`` header,
    which the operator must configure in their Paperless webhook URL
    (e.g. as a custom header). The handler loads the matching secret
    from the RLS-protected table and validates the signature.

    Returns:
        200 ``{"received": true, "tenant_id": "<uuid>"}`` on success.
        400 on signature mismatch.
        404 when no secret is configured for the tenant.
        503 when field encryption is not configured.
    """
    # Tenant comes from the custom header (public endpoint — no JWT).
    if not x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id header required for webhook routing",
        )
    try:
        tenant_uuid = uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id must be a valid UUID",
        ) from None

    if not x_paperless_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Paperless-Signature header required",
        )

    raw_body = await request.body()

    # Load the per-tenant secret from the DB. The table has FORCE ROW
    # LEVEL SECURITY + a tenant_isolation policy keyed on
    # app.current_tenant — under the runtime saebooks_app role
    # (NOSUPERUSER + NOBYPASSRLS, see migration 0056_split_db_role and
    # docs/db-role-split.md) the policy is enforced, so we MUST set
    # the GUC before the SELECT or every webhook 404s.
    #
    # The webhook has no JWT/Bearer (it's authenticated by HMAC after
    # the lookup), so we don't go through get_session — we bind
    # the tenant explicitly here. Lane 5 P0-005 / Lane 4 P0-1.
    #
    # SET LOCAL does NOT accept bindparams in Postgres ("syntax
    # error at or near "$1""); use literal interpolation, safe
    # because tenant_uuid is a typed UUID. Matches the pattern in
    # saebooks/api/v1/deps.py:_set_current_tenant_on_begin.
    from saebooks.models.integrations import PaperlessWebhookSecret

    async with AsyncSessionLocal() as session:
        # SET LOCAL needs an open transaction; the first statement on
        # an asyncpg connection in SQLAlchemy auto-begins.
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_uuid}'")
        )
        result = await session.execute(
            select(PaperlessWebhookSecret).where(
                PaperlessWebhookSecret.tenant_id == tenant_uuid,
            ).limit(1)
        )
        secret_row = result.scalars().first()

    if secret_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Paperless webhook secret configured for this tenant",
        )

    # Decrypt the stored secret.
    try:
        plaintext_secret = decrypt_field(
            secret_row.secret_ciphertext.decode("ascii")
            if isinstance(secret_row.secret_ciphertext, (bytes, bytearray))
            else secret_row.secret_ciphertext
        )
    except FieldEncryptionNotConfiguredError as exc:
        logger.error("integrations: field encryption not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret decryption unavailable — SAEBOOKS_FIELD_ENCRYPTION_KEY not set",
        ) from exc

    # Validate the signature. Two accepted forms, both compared in constant
    # time:
    #   1. ``sha256=<hex>`` — HMAC-SHA256 of the raw body with the shared
    #      secret. Used by HMAC-capable senders (a signing proxy, n8n, our own
    #      tests). Tamper-evident per body — the strongest form.
    #   2. the raw shared secret — a static bearer carried in the header.
    #      Paperless-ngx (<=2.20) CANNOT HMAC the body it sends; its workflow
    #      webhook action emits only *static* headers (see
    #      documents/workflows/actions.py — headers are passed through
    #      verbatim, no Jinja). So the native Paperless->books path configures
    #      the secret as a plain ``X-Paperless-Signature: <secret>`` header.
    #      This is a bearer over an internal-only LAN hop: Paperless and the
    #      API share the docker host and the webhook is not exposed through the
    #      public edge. Blast radius stays contained either way — ingest is
    #      DRAFT-only, idempotent on PL-<docid>, and never touches the GL (see
    #      services/integrations/paperless_ingest.ingest_document).
    presented = x_paperless_signature.strip()
    expected_hmac = "sha256=" + hmac.new(
        plaintext_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if presented.startswith("sha256="):
        sig_ok = hmac.compare_digest(expected_hmac, presented)
    else:
        sig_ok = hmac.compare_digest(plaintext_secret, presented)

    if not sig_ok:
        logger.warning(
            "integrations: paperless webhook signature mismatch for tenant=%s",
            tenant_uuid,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signature verification failed",
        )

    logger.info(
        "integrations: paperless webhook accepted for tenant=%s",
        tenant_uuid,
    )

    # --- Ingest → DRAFT bill (review-before-post; never touches the GL). ---
    # Fail-safe: any error is logged and swallowed (return 200) so Paperless
    # does not retry-storm; the source document is unharmed in Paperless and
    # can be re-triggered. The session is tenant-bound via session.info so the
    # after_begin listener re-applies app.current_tenant across create_draft's
    # internal commit (RLS stays enforced — no cross-tenant write).
    try:
        payload = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}
    doc_id = extract_document_id(payload) if isinstance(payload, dict) else None
    ingest: dict = {"status": "skipped_no_document_id"}
    if doc_id is not None:
        try:
            async with AsyncSessionLocal() as ing_session:
                ing_session.info["tenant_id"] = tenant_uuid
                ingest = await ingest_document(
                    ing_session,
                    tenant_id=tenant_uuid,
                    document_id=doc_id,
                    settings=settings,
                )
        except Exception as exc:
            logger.exception(
                "integrations: paperless ingest failed tenant=%s doc=%s",
                tenant_uuid, doc_id,
            )
            ingest = {"status": "error", "detail": str(exc)}

    return JSONResponse(
        {"received": True, "tenant_id": str(tenant_uuid), "ingest": ingest}
    )


# ---------------------------------------------------------------------------
# LEI lookup
# ---------------------------------------------------------------------------


@router.post(
    "/lei/lookup",
    summary="Look up an LEI from GLEIF",
    dependencies=[Depends(require_feature(FLAG_LEI_LOOKUP))],
)
async def lei_lookup(
    body: LeiLookupRequest,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Look up an LEI (Legal Entity Identifier) from the GLEIF registry.

    Body: ``{search: "<lei or name>"}``

    Returns the matching entity's name, address, jurisdiction, and
    registration status.

    Raises:
        404 when no entity matches the search term.
        400 on upstream LEI API error.
        503 when GLEIF is unreachable.
    """
    try:
        result = await lookup_lei(body.search, settings=settings)
    except LeiNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except LeiError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Convert dataclass to dict for JSON serialisation.
    import dataclasses
    return JSONResponse(dataclasses.asdict(result) if dataclasses.is_dataclass(result) else dict(result))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Companies House search
# ---------------------------------------------------------------------------


@router.post(
    "/companies-house/search",
    summary="Search UK Companies House",
    dependencies=[Depends(require_feature(FLAG_COMPANIES_HOUSE))],
)
async def companies_house_search(
    body: CompaniesHouseSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Search the UK Companies House register.

    Body: ``{query: "<company name or number>"}``

    Returns a list of matching companies with registration number,
    name, address, and status.

    Raises:
        404 when no company matches the query.
        503 when CH_API_KEY is not configured.
        400 on upstream Companies House API error.
    """
    try:
        result = await lookup_company(body.query, settings=settings)
    except CompaniesHouseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CompaniesHouseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except CompaniesHouseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    import dataclasses
    return JSONResponse(dataclasses.asdict(result) if dataclasses.is_dataclass(result) else dict(result))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ATO prefill (stub — Batch KK)
# ---------------------------------------------------------------------------


@router.post(
    "/ato/prefill",
    summary="ATO BAS prefill (stub — Batch KK)",
)
async def ato_prefill(
    body: AtoPrefillRequest,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Prefill BAS data from ATO's SBR endpoint.

    Currently a stub — returns 501 until the Machine Credential
    onboarding (Batch KK) is complete.  No feature flag is required
    because the stub is harmless to expose at any tier; the live
    implementation will enforce FLAG_ATO_SBR.

    Body: ``{period_start: "YYYY-MM-DD", period_end: "YYYY-MM-DD"}``
    """
    from datetime import date as _date

    try:
        ps = _date.fromisoformat(body.period_start)
        pe = _date.fromisoformat(body.period_end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date format: {exc}",
        ) from exc

    try:
        result = await prefill_bas(period_start=ps, period_end=pe)
    except AtoPrefillNotImplementedError as exc:
        return JSONResponse(
            {
                "error": "Not implemented",
                "detail": str(exc),
            },
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )

    import dataclasses
    return JSONResponse(dataclasses.asdict(result))


__all__ = ["router"]
