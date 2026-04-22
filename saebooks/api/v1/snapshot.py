"""``GET /api/v1/snapshot`` — NDJSON dump of current server state.

Phase 0 scope: contacts only. Phase 1 broadens to every entity the API
owns. Clients call this once on bootstrap, then switch to
``/api/v1/changes?since=<cursor>`` for incremental updates.

The response's last line is a ``{"_cursor": <id>}`` marker carrying the
change_log ``id`` at the moment the snapshot was read; clients seed
their local cursor from that value.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select

from saebooks.api.v1.auth import require_bearer
from saebooks.db import AsyncSessionLocal
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact

router = APIRouter(
    prefix="/snapshot",
    tags=["sync"],
    dependencies=[Depends(require_bearer)],
)


@router.get("")
async def snapshot() -> Response:
    async with AsyncSessionLocal() as session:
        # Read the current change_log head first so the cursor we emit
        # is guaranteed to be <= the last change visible in the snapshot.
        max_id = (
            await session.execute(select(func.coalesce(func.max(ChangeLog.id), 0)))
        ).scalar_one()

        contacts = (
            await session.execute(select(Contact).order_by(Contact.created_at))
        ).scalars().all()

    lines: list[str] = []
    for c in contacts:
        lines.append(
            json.dumps(
                {
                    "entity": "contact",
                    "id": str(c.id),
                    "company_id": str(c.company_id),
                    "name": c.name,
                    "contact_type": c.contact_type.value,
                    "email": c.email,
                    "phone": c.phone,
                    "abn": c.abn,
                    "address_line1": c.address_line1,
                    "address_line2": c.address_line2,
                    "city": c.city,
                    "state": c.state,
                    "postcode": c.postcode,
                    "country": c.country,
                    "notes": c.notes,
                    "default_account_id": (
                        str(c.default_account_id) if c.default_account_id else None
                    ),
                    "default_tax_code": c.default_tax_code,
                    "version": c.version,
                    "archived_at": c.archived_at.isoformat() if c.archived_at else None,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                },
                separators=(",", ":"),
            )
        )
    lines.append(json.dumps({"_cursor": max_id}, separators=(",", ":")))
    body = "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"X-Cursor-Next": str(max_id)},
    )
