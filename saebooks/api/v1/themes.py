"""Theme catalogue — ``GET /api/v1/themes`` (Wave B / FLAG_THEMES).

Route-level gate: the whole endpoint 404s below Offline (CHARTER §12.1
"All themes (MYOB Classic, SS, TT, etc.)" is an Offline+ line item) —
same 404-not-403 convention as every other ``require_feature`` gate. A
Community install can't enumerate paid-tier theme names any more than it
can browse any other paid-tier route.

This does NOT mean Community users have no working theme: the single
``"default"`` stock theme (CHARTER §6.1 "stock theme only") is always
selectable — see ``api/v1/users.py``'s ``_gate_non_default_theme``, which
gates only the SET path for a *non-default* value, not "default" itself.

The theme CSS/rendering itself lives in saebooks-web — out of scope here;
this endpoint only publishes the canonical id/label catalogue so the web
app doesn't have to hardcode a duplicate copy of the allow-list in
``services/theme.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from saebooks.api.v1.auth import require_bearer
from saebooks.services.features import FLAG_THEMES, require_feature
from saebooks.services.theme import theme_catalog_payload

router = APIRouter(
    prefix="/themes",
    tags=["themes"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_THEMES)),
    ],
)


@router.get("")
async def list_themes() -> list[dict[str, str]]:
    """Return the full theme catalogue as ``[{"id": ..., "label": ...}]``."""
    return theme_catalog_payload()
