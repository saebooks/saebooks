"""Abstract base + result dataclasses for lodgement.

The interface mirrors the lodge-server contract one-to-one — one
method per route. The signatures take pre-built envelope bytes plus
caller-supplied id (UUID for idempotency) plus a free-form metadata
dict that lodge-server passes through to the audit log.

Why bytes for the envelope
--------------------------

The customer-side STP/BAS module already generates SBR3 envelope XML
(or SuperStream MIG XML); that builder lives in ``jurisdictions/au/ato_sbr/``
today and is orthogonal to this relay. Keeping the interface as raw
bytes means this service makes no assumption about envelope schema
or version — the lodge-server validates by hashing what it receives.

Why no DB session
-----------------

This service is a thin HTTP client. Persisting receipts to a local
audit table is the caller's job (the STP UI router will do it).
We keep this layer free of SQLAlchemy so it stays trivial to unit-
test and so the same code path is reachable from cron jobs that
don't carry a request session.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class LodgementStatus(str, Enum):  # noqa: UP042  str-mixin kept: backs SQLAlchemy Enum column / str() semantics
    """Coarse outcome of a single lodgement attempt.

    Mirrors the lodge-server contract's status field plus a STUB
    sentinel for the 501 stub-mode response. Callers translate to
    UI strings; we keep the enum small so everywhere that branches
    on it must handle every value.
    """

    ACCEPTED = "accepted"
    QUEUED = "queued"
    STUB = "stub"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class LodgementResult:
    """What a successful (or "successfully queued") lodgement returns.

    Hard failures raise typed exceptions instead — a ``LodgementResult``
    in hand means the lodge-server accepted the envelope into its
    pipeline. ``ato_receipt_id`` and ``ato_timestamp`` may be None
    while in QUEUED state (the ATO hasn't issued a receipt yet) or
    will hold the ``stub_receipt_id`` while in STUB mode.

    ``raw_response`` carries the full JSON body for the audit row so
    nothing the server returned is silently dropped.
    """

    status: LodgementStatus
    ato_receipt_id: str | None
    ato_timestamp: datetime | None
    warnings: list[str] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)


class LodgementService(ABC):
    """Abstract relay client.

    One method per lodge-server route. All methods are async because
    the concrete implementation issues an HTTP call; the Null impl
    is async too so call sites don't need to branch on type.
    """

    @abstractmethod
    async def lodge_stp(
        self,
        envelope: bytes,
        payevent_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        """POST an SBR3 STP payevent envelope.

        ``payevent_id`` is the caller-supplied UUID used by the
        server for 24h dedup. The same envelope re-submitted with
        the same payevent_id returns the cached receipt rather than
        double-lodging.
        """

    @abstractmethod
    async def lodge_bas(
        self,
        envelope: bytes,
        period_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        """POST a BAS envelope. ``period_id`` is the dedup key (eg ``2026-Q3``)."""

    @abstractmethod
    async def lodge_tpar(
        self,
        envelope: bytes,
        year_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        """POST a TPAR envelope. ``year_id`` is the financial year (eg ``FY2026``)."""

    @abstractmethod
    async def send_superstream(
        self,
        message: bytes,
        message_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        """POST a SuperStream contribution message."""

    @abstractmethod
    async def poll_status(
        self,
        *,
        receipt_ref: str,
        product: str,
        metadata: dict[str, Any] | None = None,
    ) -> LodgementResult:
        """Retrieve the current ATO status for a previously-lodged envelope.

        Used to reconcile a QUEUED (deferred) lodgement: ``lodge_*`` may
        return ``QUEUED`` when the ATO has not yet issued a final receipt,
        leaving the callers record in an in-flight state. ``poll_status``
        resolves it later.

        ``receipt_ref`` is the correlation handle the caller holds — the
        ``payevent_id`` (idempotency key, == the submission id) it lodged
        under, or the ``ato_receipt_id`` if one was issued. ``product``
        distinguishes the envelope family (``"stp"`` / ``"bas"`` / ``"tpar"``)
        so a single status route can fan out server-side.

        Returns a ``LodgementResult`` whose ``status`` reflects the current
        ATO outcome: ``ACCEPTED`` (final receipt now present), ``QUEUED``
        (still deferred — caller leaves the record in-flight and re-polls
        later), or ``STUB``. An ATO *rejection* surfaces as a
        ``LodgementRejected`` exception, mirroring the lodge path.

        NOTE: the concrete remote implementation is gated on the ATO PVT
        pack + the lodge-server status route, which are not yet contracted.
        """

    @abstractmethod
    async def lookup_abr(self, abn: str) -> dict[str, Any]:
        """Resolve an ABN against the ABR via SAE Engineering's quota."""

    @abstractmethod
    async def my_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the audit rows the lodge-server has for this licence."""
