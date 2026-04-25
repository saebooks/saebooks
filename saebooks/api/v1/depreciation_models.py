"""JSON router — ``/api/v1/depreciation_models``.

Read-only endpoint for the seeded depreciation-model catalogue.

The table is populated by the AU seed CSV and is not user-editable — no
POST/PATCH/DELETE routes are exposed.  The primary consumer is the
fixed-asset create/edit form which needs a ``<select>`` dropdown backed
by real data rather than a raw slug text input.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* ``limit``/``offset`` pagination (default limit=100).
* No company scoping — depreciation models are global (no company_id column).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import DepreciationModelListOut, DepreciationModelOut
from saebooks.models.depreciation_model import DepreciationModel

router = APIRouter(
    prefix="/depreciation_models",
    tags=["depreciation_models"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=DepreciationModelListOut)
async def list_depreciation_models(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> DepreciationModelListOut:
    """Return all depreciation models ordered by id.

    The catalogue is seeded and stable; no filtering is needed beyond
    pagination.  All rows are returned — there is no archived_at column
    on this table.
    """
    total = (
        await session.execute(
            select(func.count()).select_from(DepreciationModel)
        )
    ).scalar_one()

    items = list(
        (
            await session.execute(
                select(DepreciationModel)
                .order_by(DepreciationModel.id)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return DepreciationModelListOut(
        items=[DepreciationModelOut.model_validate(m) for m in items],
        total=total,
        limit=limit,
        offset=offset,
    )
