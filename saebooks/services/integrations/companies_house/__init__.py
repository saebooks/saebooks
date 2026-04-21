"""Companies House (UK) enrichment — Enterprise-gated, same shape as LEI/ABR.

Public surface re-exports the two-step flow: ``lookup_company(number)``
returns a parsed :class:`CompaniesHouseLookup`; ``apply_to_contact``
merges it into a Contact record.

Gate: ``FLAG_COMPANIES_HOUSE`` in ``saebooks.services.features``.
Router uses ``Depends(require_feature(FLAG_COMPANIES_HOUSE))`` so
Community builds 404 on the routes even when the module is importable.
"""
from saebooks.services.integrations.companies_house.client import (
    CompaniesHouseError,
    CompaniesHouseNotConfiguredError,
    CompaniesHouseNotFoundError,
    lookup_company_raw,
)
from saebooks.services.integrations.companies_house.enrich import (
    CompaniesHouseLookup,
    apply_to_contact,
    lookup_company,
    parse_company_response,
)

__all__ = [
    "CompaniesHouseError",
    "CompaniesHouseLookup",
    "CompaniesHouseNotConfiguredError",
    "CompaniesHouseNotFoundError",
    "apply_to_contact",
    "lookup_company",
    "lookup_company_raw",
    "parse_company_response",
]
