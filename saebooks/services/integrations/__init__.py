"""External integrations: Paperless, LEI/GLEIF, Stripe, ATO Prefill.

Each submodule is a thin wrapper around a third-party HTTP surface.
They are intentionally isolated from the core business logic — the
core doesn't import from here; routers + CLI jobs do.

Common shape:

* Every client raises a module-local error hierarchy (``*NotConfiguredError``
  for missing creds, ``*Error`` for upstream failures).
* Every client takes an optional ``httpx.AsyncClient`` so tests can
  inject a respx-mocked transport.
* Paid-tier integrations (LEI) are additionally gated by
  ``saebooks.services.features`` flags at the router level.
"""
from saebooks.services.integrations.ato_prefill import (
    AtoPrefillError,
    AtoPrefillNotImplementedError,
    prefill_bas,
)
from saebooks.services.integrations.companies_house import (
    CompaniesHouseError,
    CompaniesHouseLookup,
    CompaniesHouseNotConfiguredError,
    CompaniesHouseNotFoundError,
    lookup_company,
    parse_company_response,
)
from saebooks.services.integrations.companies_house import (
    apply_to_contact as apply_ch_to_contact,
)
from saebooks.services.integrations.lei import (
    LeiError,
    LeiLookup,
    LeiNotFoundError,
    apply_to_contact,
    lookup_lei,
    parse_lei_response,
)
from saebooks.services.integrations.paperless import (
    PaperlessAttachment,
    PaperlessClient,
    PaperlessError,
    PaperlessNotConfiguredError,
    attach_to_journal,
    build_browser_url,
)
from saebooks.services.integrations.stripe import (
    StripeError,
    StripeNotConfiguredError,
    StripeSignatureError,
    handle_payment_intent_succeeded,
    verify_signature,
)
from saebooks.services.integrations.stripe_links import create_payment_link

__all__ = [
    "AtoPrefillError",
    "AtoPrefillNotImplementedError",
    "CompaniesHouseError",
    "CompaniesHouseLookup",
    "CompaniesHouseNotConfiguredError",
    "CompaniesHouseNotFoundError",
    "LeiError",
    "LeiLookup",
    "LeiNotFoundError",
    "PaperlessAttachment",
    "PaperlessClient",
    "PaperlessError",
    "PaperlessNotConfiguredError",
    "StripeError",
    "StripeNotConfiguredError",
    "StripeSignatureError",
    "apply_ch_to_contact",
    "apply_to_contact",
    "attach_to_journal",
    "build_browser_url",
    "create_payment_link",
    "handle_payment_intent_succeeded",
    "lookup_company",
    "lookup_lei",
    "parse_company_response",
    "parse_lei_response",
    "prefill_bas",
    "verify_signature",
]
