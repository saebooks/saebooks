"""Internal endpoint: mint the next document number for a (company, kind).

``POST /internal/numbering/next`` — a thin wrapper over
``services.numbering.next_number`` exposed so a sibling *module* container
(the #32 pre-accounting module) can consume the ONE shared numbering
authority instead of owning its own ``document_counters`` table.

INTERNAL ONLY
-------------
Mounts under ``/internal`` (outside ``/api/v1``, stripped from the public
OpenAPI, unreachable from the public edge) and is gated by
``require_internal_token`` (``X-Internal-Token`` vs ``INTERNAL_API_TOKEN``,
fail-closed 503 when unset — see ``api/internal/auth.py``).

Gap semantics (IMPORTANT)
-------------------------
``next_number`` is gap-free only while the counter-row lock lives inside the
*caller's* transaction. Here the number is committed by THIS request; if the
module's follow-up write then fails, the number is burned (a gap). That is
acceptable for commitment/offer documents (``quote``, ``purchase_order``)
which are not tax documents, but NOT for statutory gap-free sequences
(``invoice``, ``credit_note``, ``receipt``) — those must be minted in the
same transaction as the ledger row by the engine itself. So this endpoint
REJECTS the gap-sensitive kinds with 422; they never route through here.

Session role
------------
Uses the BYPASSRLS owner session (``LoginSessionLocal``): the caller is a
trusted module authenticated by the internal token and passes only
``{company_id, kind}`` — there is no JWT / ``app.current_tenant`` to bind, so
the RLS predicate on ``document_counters`` cannot be satisfied under the app
role. This mirrors the ephemeral-demo internal path. The company's existence
is validated explicitly (404) so a bad id is a clean error, not an FK 500.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from saebooks.api.internal.auth import require_internal_token
from saebooks.db import LoginSessionLocal
from saebooks.models.company import Company
from saebooks.services import numbering

logger = logging.getLogger("saebooks.api.internal.numbering")

router = APIRouter(
    prefix="/numbering",
    tags=["internal"],
    dependencies=[Depends(require_internal_token)],
)

# Statutory gap-free kinds that must be minted in-transaction by the engine
# and therefore may NOT be served over this (gap-permitting) endpoint.
_GAP_FREE_KINDS: frozenset[str] = frozenset({"invoice", "credit_note", "receipt"})


class NextNumberRequest(BaseModel):
    company_id: UUID
    kind: str


@router.post("/next")
async def next_number(body: NextNumberRequest) -> JSONResponse:
    """Advance the (company, kind) counter and return ``{"number": "<n>"}``."""
    kind = body.kind.strip()

    if kind in _GAP_FREE_KINDS:
        return JSONResponse(
            {
                "error": "gap_free_kind",
                "message": (
                    f"kind '{kind}' requires a gap-free statutory sequence and "
                    "must be numbered in-transaction by the engine, not via the "
                    "internal numbering endpoint"
                ),
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if kind not in numbering.KNOWN_KINDS:
        return JSONResponse(
            {"error": "unknown_kind", "message": f"unknown document kind '{kind}'"},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    async with LoginSessionLocal() as session:
        company = (
            await session.execute(
                select(Company.id).where(Company.id == body.company_id)
            )
        ).scalar_one_or_none()
        if company is None:
            return JSONResponse(
                {"error": "company_not_found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )

        number = await numbering.next_number(session, body.company_id, kind)
        # The number is durable once THIS request commits (see gap semantics
        # in the module docstring).
        await session.commit()

    return JSONResponse({"number": number}, status_code=status.HTTP_200_OK)
