"""Fixed-asset API service — CRUD + search + archive.

API-tier functions for ``/api/v1/fixed_assets`` with optimistic locking
+ change_log. The lower-level depreciation and disposal logic lives in
``saebooks.services.assets`` — this module is strictly the CRUD/listing
layer that the v1 API router calls.

Status values on FixedAsset: ``active``, ``disposed``, ``archived``
(lowercase, matching the column server default).

Post-disposal restriction:
    Once status is ``disposed`` only cosmetic fields (description, extra)
    may be changed via PATCH. All other field changes on a disposed asset
    raise ``FixedAssetApiError``.

Archive restriction:
    An ``active`` asset with remaining book value (``cost > 0`` and not
    fully depreciated against ``cost - residual_value``) cannot be
    archived — the caller must dispose first. A ``disposed`` asset can
    be archived freely.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.depreciation_model import DepreciationModel
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import change_log as change_log_svc
from saebooks.services.numbering import next_number

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Fields serialised into change_log.payload for fixed_asset operations.
_ASSET_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "code",
    "name",
    "description",
    "status",
    "depreciation_model_id",
    "tax_model_id",
    "cost_account_id",
    "accum_dep_account_id",
    "dep_expense_account_id",
    "purchase_date",
    "in_service_date",
    "cost",
    "residual_value",
    "last_depreciation_posted_through",
    "disposal_date",
    "disposal_proceeds",
    "serial_number",
    "manufacturer",
    "model_number",
    "location",
    "custody_person",
    "warranty_end",
    "extra",
    "version",
    "created_at",
    "archived_at",
)

# Fields that remain editable after disposal (cosmetic only).
_POST_DISPOSAL_MUTABLE = frozenset({"description", "extra"})

# Fields that can be changed via PATCH on an active asset.
_ALLOWED_UPDATE_FIELDS = frozenset({
    "name",
    "description",
    "depreciation_model_id",
    "tax_model_id",
    "purchase_date",
    "in_service_date",
    "residual_value",
    "serial_number",
    "manufacturer",
    "model_number",
    "location",
    "custody_person",
    "warranty_end",
    "extra",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FixedAssetApiError(ValueError):
    """Raised on validation or state-transition failure (API tier)."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: FixedAsset) -> None:
        super().__init__(
            f"FixedAsset {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise(asset: FixedAsset) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _ASSET_COLUMNS:
        val = getattr(asset, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "isoformat"):  # date
            val = val.isoformat()
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_fixed_assets(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    depreciation_model_id: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[FixedAsset], int]:
    """Return (assets, total_count) filtered by status/model/archived."""
    where = [FixedAsset.company_id == company_id]

    if not archived:
        where.append(FixedAsset.archived_at.is_(None))
    else:
        where.append(FixedAsset.archived_at.isnot(None))

    if status is not None:
        where.append(FixedAsset.status == status)

    if depreciation_model_id is not None:
        where.append(FixedAsset.depreciation_model_id == depreciation_model_id)

    count_stmt = select(sa_func.count()).select_from(FixedAsset).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(FixedAsset)
        .options(selectinload(FixedAsset.depreciation_model))
        .where(*where)
        .order_by(FixedAsset.code)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def api_get(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> FixedAsset | None:
    """Fetch a single fixed asset by primary key (with depreciation model eager-loaded)."""
    stmt = (
        select(FixedAsset)
        .options(selectinload(FixedAsset.depreciation_model))
        .where(FixedAsset.id == asset_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    name: str,
    depreciation_model_id: str,
    cost_account_id: uuid.UUID,
    accum_dep_account_id: uuid.UUID,
    dep_expense_account_id: uuid.UUID,
    purchase_date: Any,
    cost: Decimal,
    in_service_date: Any | None = None,
    residual_value: Decimal | None = None,
    code: str | None = None,
    description: str | None = None,
    tax_model_id: str | None = None,
    serial_number: str | None = None,
    manufacturer: str | None = None,
    model_number: str | None = None,
    location: str | None = None,
    custody_person: str | None = None,
    warranty_end: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> FixedAsset:
    """Create a new fixed asset row with version=1 and change_log entry.

    ``code`` auto-assigns to ``AST-NNNNNN`` format if not supplied.
    ``depreciation_model_id`` is required — raises if missing or invalid.
    """
    # Validate the depreciation model exists.
    dep_model = await session.get(DepreciationModel, depreciation_model_id)
    if dep_model is None:
        raise FixedAssetApiError(
            f"depreciation_model_id '{depreciation_model_id}' does not exist"
        )

    # Auto-assign code if not supplied.
    resolved_code = code.strip() if code else await next_number(
        session, company_id, "fixed_asset"
    )

    asset = FixedAsset(
        company_id=company_id,
        tenant_id=tenant_id,
        code=resolved_code,
        name=name.strip(),
        description=description,
        depreciation_model_id=depreciation_model_id,
        tax_model_id=tax_model_id,
        cost_account_id=cost_account_id,
        accum_dep_account_id=accum_dep_account_id,
        dep_expense_account_id=dep_expense_account_id,
        purchase_date=purchase_date,
        in_service_date=in_service_date or purchase_date,
        cost=Decimal(str(cost)).quantize(Decimal("0.01")),
        residual_value=(
            Decimal(str(residual_value)).quantize(Decimal("0.01"))
            if residual_value is not None
            else Decimal("0.00")
        ),
        serial_number=serial_number,
        manufacturer=manufacturer,
        model_number=model_number,
        location=location,
        custody_person=custody_person,
        warranty_end=warranty_end,
        extra=extra,
        status="active",
        version=1,
    )
    session.add(asset)
    await session.flush()
    await session.refresh(asset)

    await change_log_svc.append(
        session,
        entity="fixed_asset",
        entity_id=asset.id,
        op="created",
        actor=actor,
        payload=_serialise(asset),
        version=asset.version,
    )
    await session.commit()
    # Reload with depreciation model for response.
    return await api_get(session, asset.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    asset_id: uuid.UUID,
    actor: str,
    expected_version: int,
    **kwargs: Any,
) -> FixedAsset:
    """Update fixed asset fields with optimistic locking + change_log.

    If the asset is ``disposed``, only ``description`` and ``extra`` may
    be changed. Any other field in kwargs raises ``FixedAssetApiError``.
    """
    asset = await api_get(session, asset_id)
    if asset is None:
        raise FixedAssetApiError(f"FixedAsset {asset_id} not found")
    if asset.version != expected_version:
        raise VersionConflict(asset)

    if asset.status == "disposed":
        non_cosmetic = set(kwargs.keys()) - _POST_DISPOSAL_MUTABLE
        if non_cosmetic:
            raise FixedAssetApiError(
                f"Cannot modify {sorted(non_cosmetic)} on a disposed asset — "
                "only description and extra are mutable post-disposal"
            )

    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            raise FixedAssetApiError(f"Unknown or non-editable field: {key}")
        if key == "depreciation_model_id" and value is not None:
            dep_model = await session.get(DepreciationModel, value)
            if dep_model is None:
                raise FixedAssetApiError(
                    f"depreciation_model_id '{value}' does not exist"
                )
        if value is not None and key in ("cost", "residual_value"):
            value = Decimal(str(value)).quantize(Decimal("0.01"))
        if value is not None and key == "name":
            value = value.strip()
        if value is not None and key == "description":
            value = value  # pass through unchanged
        setattr(asset, key, value)

    asset.version = asset.version + 1
    await session.flush()
    await session.refresh(asset)

    await change_log_svc.append(
        session,
        entity="fixed_asset",
        entity_id=asset.id,
        op="updated",
        actor=actor,
        payload=_serialise(asset),
        version=asset.version,
    )
    await session.commit()
    return await api_get(session, asset_id)  # type: ignore[return-value]


async def api_delete(
    session: AsyncSession,
    asset_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> FixedAsset:
    """Soft-archive a fixed asset with optimistic locking + change_log.

    Restrictions:
    - An ``active`` asset with remaining book value (cost > residual_value)
      cannot be archived — caller must dispose it first.
    - A ``disposed`` asset can be archived freely.
    """
    asset = await api_get(session, asset_id)
    if asset is None:
        raise FixedAssetApiError(f"FixedAsset {asset_id} not found")
    if asset.version != expected_version:
        raise VersionConflict(asset)

    if asset.status == "active" and asset.cost > asset.residual_value:
        raise FixedAssetApiError(
            "Cannot archive active asset with remaining book value — dispose first"
        )

    asset.archived_at = datetime.now(UTC)
    asset.version = asset.version + 1
    await session.flush()
    await session.refresh(asset)

    await change_log_svc.append(
        session,
        entity="fixed_asset",
        entity_id=asset.id,
        op="deleted",
        actor=actor,
        payload=_serialise(asset),
        version=asset.version,
    )
    await session.commit()
    return asset


async def api_dispose(
    session: AsyncSession,
    asset_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    disposal_date: Any,
    proceeds: Decimal,
    notes: str | None = None,
) -> FixedAsset:
    """Dispose a fixed asset with optimistic locking + change_log.

    Marks the asset as ``disposed``, stamps ``disposal_date`` and
    ``disposal_proceeds``, and bumps the version. Does NOT post GL
    journals — full accounting disposal is handled by the lower-tier
    ``saebooks.services.assets.dispose_asset`` function. This entry
    point is the thin API-tier state-transition that the v1 router calls.

    Raises:
        FixedAssetApiError: asset not found, or already disposed.
        VersionConflict: ``expected_version`` does not match stored value.
    """
    asset = await api_get(session, asset_id)
    if asset is None:
        raise FixedAssetApiError(f"FixedAsset {asset_id} not found")
    if asset.status == "disposed":
        raise FixedAssetApiError(
            f"FixedAsset {asset_id} is already disposed"
        )
    if asset.version != expected_version:
        raise VersionConflict(asset)

    asset.status = "disposed"
    asset.disposal_date = disposal_date
    asset.disposal_proceeds = Decimal(str(proceeds)).quantize(Decimal("0.01"))
    asset.version = asset.version + 1
    await session.flush()
    await session.refresh(asset)

    await change_log_svc.append(
        session,
        entity="fixed_asset",
        entity_id=asset.id,
        op="disposed",
        actor=actor,
        payload=_serialise(asset),
        version=asset.version,
    )
    await session.commit()
    return await api_get(session, asset_id)  # type: ignore[return-value]


__all__ = [
    "FixedAssetApiError",
    "VersionConflict",
    "api_create",
    "api_delete",
    "api_dispose",
    "api_get",
    "api_update",
    "list_fixed_assets",
]
