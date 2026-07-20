"""Estonian EN 16931 / Peppol BIS Billing 3.0 e-invoicing.

AGPL, engine-side (this package): the UBL Invoice serializer
(``serializer.py``), the reporting_type -> tax-category mapping
(``mapping.py``), the operator-agnostic transport interface + mock
(``operator.py``), and the DB-aware generator (``generator.py``) that turns a
posted engine ``Invoice`` into standards-compliant e-invoice XML. Mirrors the
``lodgement/kmd*`` split.

PROPRIETARY, commercial-only: the operator transport's REAL implementation
(``FinbiteTransport`` in ``operator.py`` — send/poll against a live Estonian
e-invoice operator such as Finbite over the Peppol roaming model, loud-gated
on live credentials that are not provisioned in this build). ``operator.py``
as a WHOLE is STUBbed in the public export — same mechanism as
``lodgement/adapters/ee_kmd3.py`` — see
``public-stubs/saebooks/services/einvoice/operator.py`` and
``scripts/build-public-export.py``'s ``STUB_PATHS``/``EXPECTED_SYMBOLS``. The
open engine computes and serializes the e-invoice, and its
``MockOperatorTransport`` proves the send/poll lifecycle end to end offline;
only certified network transmission is commercial.

This ``__init__.py`` deliberately re-exports only ``mapping``/``serializer``/
``operator`` — all three import NO database models, so importing this
package never touches ``saebooks.db``. ``generator.py`` IS DB-aware
(``saebooks.models.company``/``contact``/``invoice``/``tax_code``) and is
deliberately NOT re-exported here — import it directly
(``from saebooks.services.einvoice.generator import generate_einvoice``),
mirroring ``lodgement/kmd_2027/__init__.py``'s identical split (that
package's own ``__init__.py`` re-exports only its pure ``serializer.py``,
never its DB-bound ``generator.py``, for the exact same reason: a stubbed
sibling module's package-level import must not transitively require a live
DB connection just to resolve ``__all__``)."""
from __future__ import annotations

from saebooks.services.einvoice.mapping import (
    REPORTING_TYPE_TO_TAX_CATEGORY,
    TaxCategoryMapping,
    resolve_tax_category,
)
from saebooks.services.einvoice.operator import (
    DeliveryStatus,
    DeliveryStatusResult,
    EInvoiceOperatorAuthError,
    EInvoiceOperatorClient,
    EInvoiceOperatorError,
    EInvoiceOperatorLiveCredentialsMissing,
    EInvoiceOperatorNotFoundError,
    EInvoiceOperatorRecipientUnknownError,
    EInvoiceOperatorTransport,
    EInvoiceOperatorUpstreamError,
    EInvoiceOperatorValidationError,
    FinbiteConfig,
    FinbiteTransport,
    PeppolParticipantId,
    SendResult,
)
from saebooks.services.einvoice.serializer import (
    EInvoiceDocument,
    EInvoiceLine,
    EInvoiceParty,
    EInvoiceTaxSubtotal,
    build_einvoice_xml_document,
    to_bytes,
)

__all__ = [
    "REPORTING_TYPE_TO_TAX_CATEGORY",
    "DeliveryStatus",
    "DeliveryStatusResult",
    "EInvoiceDocument",
    "EInvoiceLine",
    "EInvoiceOperatorAuthError",
    "EInvoiceOperatorClient",
    "EInvoiceOperatorError",
    "EInvoiceOperatorLiveCredentialsMissing",
    "EInvoiceOperatorNotFoundError",
    "EInvoiceOperatorRecipientUnknownError",
    "EInvoiceOperatorTransport",
    "EInvoiceOperatorUpstreamError",
    "EInvoiceOperatorValidationError",
    "EInvoiceParty",
    "EInvoiceTaxSubtotal",
    "FinbiteConfig",
    "FinbiteTransport",
    "PeppolParticipantId",
    "SendResult",
    "TaxCategoryMapping",
    "build_einvoice_xml_document",
    "resolve_tax_category",
    "to_bytes",
]
