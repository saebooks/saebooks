"""Internal endpoint: provision an ephemeral per-visit demo tenant.

``POST /internal/demo/provision`` — called by the saebooks-web container over
the docker network on a fresh public-preview root visit. Mints a brand-new
isolated, seeded demo company (its own RLS tenant) + demo user + JWT and
returns a payload the web app uses verbatim as ``Authorization: Bearer
<access_token>`` for every subsequent ``/api/v1/*`` call.

INTERNAL ONLY
-------------
This router mounts at ``/internal`` (outside ``/api/v1``), is stripped from the
published OpenAPI, and the public edge (Caddy/Consul) routes only the web
container — so it is not reachable from a browser through the tunnel. As
defence-in-depth it also enforces a shared-secret guard (see ``_guard``).

Contract (what saebooks-web builds against)
-------------------------------------------
Request: POST /internal/demo/provision
  Headers:
    X-Internal-Secret: <DEMO_INTERNAL_SECRET>   (required when configured)
    X-Forwarded-For / X-Real-IP: <client ip>    (optional; used for rate-limit)
  Body (JSON, all optional):
    { "source_ip": "<client ip>" }              (overrides the header-derived IP)

Response 201 (application/json) — a strict superset of POST /api/v1/auth/login:
    {
      "access_token": "<jwt>",       # carry as Authorization: Bearer <jwt>
      "token_type": "bearer",
      "expires_in": <seconds>,
      "company_id": "<uuid>",
      "tenant_id": "<uuid>",
      "demo_user_email": "demo+<token>@saebooks.example"
    }

Errors:
    503 {"error":"demo_disabled"}      ephemeral demos switched off
    503 {"error":"demo_at_capacity"}   cap reached, nothing reapable
    429 {"error":"rate_limited"}       this IP over DEMO_PROVISION_PER_IP_PER_MIN
    403                                bad/missing X-Internal-Secret (when configured)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from saebooks.api.v1.auth import _is_dev_env
from saebooks.config import settings
from saebooks.services import ephemeral_demo

logger = logging.getLogger("saebooks.api.internal.demo")

router = APIRouter(prefix="/demo", tags=["internal"])


class ProvisionRequest(BaseModel):
    # Optional explicit client IP; if omitted we derive it from the
    # X-Forwarded-For / X-Real-IP headers the web container sets.
    source_ip: str | None = None


def _guard(secret_header: str | None) -> JSONResponse | None:
    """Shared-secret gate. Returns a 403 response to short-circuit, or None to proceed.

    * If ``DEMO_INTERNAL_SECRET`` is set: the request MUST carry a matching
      ``X-Internal-Secret`` header.
    * If it is empty AND we're in dev/test: skip the guard (the endpoint is
      already unreachable from the public edge; tests call it directly).
    * If it is empty AND we're NOT in dev/test: refuse (403) — a prod instance
      must configure the secret rather than expose the endpoint ungated.
    """
    expected = settings.demo_internal_secret.strip()
    if expected:
        import secrets as _secrets

        if not secret_header or not _secrets.compare_digest(
            secret_header.strip(), expected
        ):
            return JSONResponse(
                {"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN
            )
        return None
    # No secret configured.
    if _is_dev_env():
        return None
    logger.error(
        "demo provision rejected: DEMO_INTERNAL_SECRET unset in a non-dev env"
    )
    return JSONResponse(
        {"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN
    )


def _client_ip(request: Request, body_ip: str | None) -> str | None:
    if body_ip:
        return body_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First hop is the original client.
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    client = request.client
    return client.host if client is not None else None


@router.post("/provision")
async def provision_demo(
    request: Request,
    body: ProvisionRequest | None = None,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> JSONResponse:
    blocked = _guard(x_internal_secret)
    if blocked is not None:
        return blocked

    source_ip = _client_ip(request, body.source_ip if body else None)

    try:
        result = await ephemeral_demo.provision(source_ip=source_ip)
    except ephemeral_demo.DemoDisabled:
        return JSONResponse(
            {"error": "demo_disabled"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ephemeral_demo.DemoRateLimited:
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    except ephemeral_demo.DemoAtCapacity:
        return JSONResponse(
            {"error": "demo_at_capacity"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return JSONResponse(
        {
            "access_token": result.access_token,
            "token_type": "bearer",
            "expires_in": result.expires_in,
            "company_id": str(result.company_id),
            "tenant_id": str(result.tenant_id),
            "demo_user_email": result.demo_user_email,
        },
        status_code=status.HTTP_201_CREATED,
    )
