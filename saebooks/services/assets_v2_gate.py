"""Wave A (2026-07-10) — the ``FLAG_ASSET_V2`` create/update-time gate.

Per CHARTER, v1 (linear/no-depreciation models + full disposal) is a
Community baseline and stays ungated. Only the v2-specific asset-
register actions are paid-tier:

* selecting a diminishing-value depreciation model (``method ==
  "diminishing_value"`` on the seeded ``depreciation_models``
  catalogue) as either the book (``depreciation_model_id``) or tax
  (``tax_model_id``) model on a ``FixedAsset``;
* the tax-vs-book split itself — setting ``tax_model_id`` at all,
  regardless of which model it points at, since running two parallel
  depreciation schedules on one asset is the v2 feature even when both
  happen to be linear.

This module holds the one shared check so both the create and update
routes in ``api/v1/fixed_assets.py`` gate identically (gating create
only would let a caller create a linear-model asset, then PATCH it to
a diminishing-value model or add a tax split, unenforced).

NOT covered here (see the Wave A build report — orphaned, no route to
gate yet): ``services.assets.dispose_partial`` (partial disposal) and
``services.assets_import`` (CSV bulk import). Both are real, tested
service-layer code with zero API/web/MCP callers today — there is
nothing to attach ``require_feature`` to until one is wired up. When
either gets a route, gate it with ``require_feature`` there
(dispose_partial: unconditionally, since any partial disposal is v2;
CSV import: unconditionally, since bulk-importing a whole register is
itself a v2-scale operation) rather than reusing this module's
conditional per-field check.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.services.features import FLAG_ASSET_V2, require_feature_inline


async def gate_asset_v2_fields(
    session: AsyncSession,
    request: Request,
    *,
    depreciation_model_id: str | None,
    tax_model_id: str | None,
) -> None:
    """404 (via ``require_feature_inline``) when the request selects a
    v2 asset-register feature.

    A no-op — the common case, at every tier — for a plain single
    (book-only) linear/no-depreciation model with no tax split.
    Existence validation of the model ids themselves stays the
    service layer's job (``FixedAssetApiError`` on an unknown id);
    this only inspects the ``method`` of ids that DO resolve, so an
    invalid id still surfaces as the pre-existing 422, not a
    confusing 404.
    """
    from saebooks.models.depreciation_model import DepreciationModel

    if tax_model_id is not None:
        require_feature_inline(FLAG_ASSET_V2, request)
        return

    if depreciation_model_id is not None:
        model = await session.get(DepreciationModel, depreciation_model_id)
        if model is not None and model.method == "diminishing_value":
            require_feature_inline(FLAG_ASSET_V2, request)
