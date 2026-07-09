"""Null implementation — for community / offline editions.

Every method raises ``LodgementUnsupportedEdition``. The factory
returns this whenever ``LicenseService.has_feature("ato_sbr")`` is
False, so the call sites can stay simple — they always get *a*
``LodgementService`` and don't need an Optional-aware branch.

This is also the safety net behind ``require_feature("ato_sbr")``:
if the route-level gate is bypassed (test override, explicit dep
mismatch), the call into the lodgement service still fails loudly
with a typed exception rather than silently no-op'ing.
"""
from __future__ import annotations

from typing import Any

from saebooks.services.lodgement.base import (
    LodgementResult,
    LodgementService,
)
from saebooks.services.lodgement.exceptions import LodgementUnsupportedEdition


class NullLodgementService(LodgementService):
    """No-op service that refuses every call."""

    REQUIRED_EDITION = "pro"
    REQUIRED_FLAG = "ato_sbr"

    def _refuse(self) -> None:
        raise LodgementUnsupportedEdition(
            required_edition=self.REQUIRED_EDITION,
            flag=self.REQUIRED_FLAG,
        )

    async def lodge_stp(
        self,
        envelope: bytes,
        payevent_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def lodge_bas(
        self,
        envelope: bytes,
        period_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def lodge_tpar(
        self,
        envelope: bytes,
        year_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def send_superstream(
        self,
        message: bytes,
        message_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def poll_status(
        self,
        *,
        receipt_ref: str,
        product: str,
        metadata: dict[str, Any] | None = None,
    ) -> LodgementResult:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def lookup_abr(self, abn: str) -> dict[str, Any]:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def my_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        self._refuse()
        raise RuntimeError("unreachable")  # pragma: no cover
