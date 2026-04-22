"""``GET /api/v1/changes`` — NDJSON stream of change_log rows.

Offline desktop clients poll this endpoint with
``?since=<cursor>&limit=500`` and advance their local cursor from the
``X-Cursor-Next`` response header.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from saebooks.api.v1.auth import require_bearer
from saebooks.db import AsyncSessionLocal
from saebooks.services import change_log as change_log_svc

router = APIRouter(
    prefix="/changes",
    tags=["sync"],
    dependencies=[Depends(require_bearer)],
)


def _serialise_row(row) -> str:
    """Render one ChangeLog row as a single NDJSON line."""
    return json.dumps(
        {
            "id": row.id,
            "entity": row.entity,
            "entity_id": str(row.entity_id),
            "op": row.op,
            "actor": row.actor,
            "at": row.at.isoformat() if row.at else None,
            "version": row.version,
            "payload": row.payload,
        },
        separators=(",", ":"),
    )


@router.get("")
async def stream_changes(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    entity: str | None = Query(default=None),
) -> Response:
    async with AsyncSessionLocal() as session:
        rows = await change_log_svc.since(
            session, cursor=since, limit=limit, entity=entity
        )
    body_lines = [_serialise_row(r) for r in rows]
    body = ("\n".join(body_lines) + ("\n" if body_lines else ""))
    next_cursor = rows[-1].id if rows else since
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "X-Cursor-Next": str(next_cursor),
            "X-Row-Count": str(len(rows)),
        },
    )
