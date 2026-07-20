"""Estonian e-invoice operator transport — PUBLIC SHIM (transmission stubbed).

The private build implements the operator-agnostic send/poll transport
interface (``EInvoiceOperatorTransport``) plus a real Finbite Access Point
adapter (``FinbiteTransport``, itself loud-gated — no operator account is
provisioned in the private build either, see that module's own docstring).
Certified transmission over the Peppol network is a commercial feature and is
NOT shipped in the open repo — the open engine computes and serializes a
standards-compliant EN 16931/Peppol BIS 3.0 e-invoice (``mapping.py``/
``serializer.py``/``generator.py``, all three fully public, unstubbed) but
does not transmit it.

Symbols preserved exactly (every exception class, every dataclass field,
the ``EInvoiceOperatorTransport`` ABC, ``EInvoiceOperatorClient``'s
constructor/method signatures, ``PeppolParticipantId``, ``FinbiteConfig``) so
any code that imports this module keeps working; every transport-touching
call raises ``NotImplementedError("commercial feature")`` instead of doing
real I/O. ``MockOperatorTransport`` is NOT shipped here — it exists only to
drive the private build's own test suite, which is not part of the open repo
either (mirrors ``lodgement/adapters/ee_kmd3.py``'s public shim's identical
omission of ``MockKmd3Transport``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

_COMMERCIAL = (
    "commercial feature: certified Peppol network transmission of an "
    "e-invoice is not available in the open engine — the community build "
    "computes and serializes the EN 16931/Peppol BIS 3.0 UBL Invoice but "
    "does not send it"
)


class EInvoiceOperatorError(Exception):
    pass


class EInvoiceOperatorValidationError(EInvoiceOperatorError):
    pass


class EInvoiceOperatorAuthError(EInvoiceOperatorError):
    pass


class EInvoiceOperatorRecipientUnknownError(EInvoiceOperatorError):
    pass


class EInvoiceOperatorUpstreamError(EInvoiceOperatorError):
    pass


class EInvoiceOperatorNotFoundError(EInvoiceOperatorError):
    pass


class EInvoiceOperatorLiveCredentialsMissing(EInvoiceOperatorError):
    def __init__(
        self,
        detail: str = (
            "No Estonian e-invoice operator account is configured — refusing "
            "to open a connection. Inject a transport for offline tests "
            "(MockOperatorTransport), or provision real operator API "
            "credentials to go live."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PeppolParticipantId:
    scheme: str
    value: str

    def as_string(self) -> str:
        return f"{self.scheme}:{self.value}"


class DeliveryStatus(str, Enum):  # noqa: UP042
    SENT = "sent"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SendResult:
    transmission_id: str
    status: DeliveryStatus
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryStatusResult:
    transmission_id: str
    status: DeliveryStatus
    detail: str | None = None
    updated_at: datetime | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


class EInvoiceOperatorTransport(ABC):
    @abstractmethod
    async def send_invoice(
        self,
        xml_bytes: bytes,
        *,
        sender: PeppolParticipantId,
        recipient: PeppolParticipantId,
        document_id: str,
        idempotency_id: str,
    ) -> SendResult: ...

    @abstractmethod
    async def poll_delivery_status(self, transmission_id: str) -> DeliveryStatusResult: ...


class EInvoiceOperatorClient:
    """Jurisdiction='EE' e-invoice operator client — send/poll stubbed in the open engine."""

    def __init__(self, transport: EInvoiceOperatorTransport, *, sender: PeppolParticipantId) -> None:
        self._transport = transport
        self._sender = sender

    @property
    def sender(self) -> PeppolParticipantId:
        return self._sender

    async def send(
        self,
        xml_bytes: bytes,
        *,
        recipient: PeppolParticipantId,
        document_id: str,
        idempotency_id: str,
    ) -> SendResult:
        raise NotImplementedError(_COMMERCIAL)

    async def poll(self, transmission_id: str) -> DeliveryStatusResult:
        raise NotImplementedError(_COMMERCIAL)


@dataclass(frozen=True, slots=True)
class FinbiteConfig:
    api_base_url: str | None
    api_key: str | None

    def is_complete(self) -> bool:
        return bool(self.api_base_url and self.api_key)


class FinbiteTransport(EInvoiceOperatorTransport):
    def __init__(self, config: FinbiteConfig | None = None) -> None:
        self._config = config

    async def send_invoice(
        self,
        xml_bytes: bytes,
        *,
        sender: PeppolParticipantId,
        recipient: PeppolParticipantId,
        document_id: str,
        idempotency_id: str,
    ) -> SendResult:
        raise NotImplementedError(_COMMERCIAL)

    async def poll_delivery_status(self, transmission_id: str) -> DeliveryStatusResult:
        raise NotImplementedError(_COMMERCIAL)
